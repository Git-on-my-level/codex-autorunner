from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import re
import secrets
import shlex
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime
from os import getenv
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Sequence

import httpx

from ....agents.opencode.client import OpenCodeProtocolError
from ....agents.opencode.harness import OpenCodeHarness
from ....agents.opencode.runtime import (
    PERMISSION_ALLOW,
    PERMISSION_ASK,
    OpenCodeTurnOutput,
    build_turn_id,
    collect_opencode_output,
    extract_session_id,
    format_permission_prompt,
    map_approval_policy_to_permission,
    opencode_missing_env,
    parse_message_response,
    split_model_id,
)
from ....agents.opencode.supervisor import OpenCodeSupervisorError
from ....core.config import load_hub_config, load_repo_config
from ....core.injected_context import wrap_injected_context
from ....core.logging_utils import log_event
from ....core.state import now_iso
from ....core.update import _normalize_update_target, _spawn_update_process
from ....core.utils import canonicalize_path, resolve_opencode_binary
from ....integrations.github.service import GitHubError, GitHubService
from ....manifest import load_manifest
from ...app_server.client import (
    CodexAppServerClient,
    CodexAppServerDisconnected,
    CodexAppServerError,
    _normalize_sandbox_policy,
)
from ..adapter import (
    CompactCallback,
    InlineButton,
    PrFlowStartCallback,
    TelegramCallbackQuery,
    TelegramCommand,
    TelegramMessage,
    build_compact_keyboard,
    build_inline_keyboard,
    build_update_confirm_keyboard,
    encode_cancel_callback,
)
from ..config import AppServerUnavailableError, TelegramMediaCandidate
from ..constants import (
    AGENT_PICKER_PROMPT,
    APPROVAL_POLICY_VALUES,
    APPROVAL_PRESETS,
    BIND_PICKER_PROMPT,
    COMMAND_DISABLED_TEMPLATE,
    COMPACT_SUMMARY_PROMPT,
    DEFAULT_AGENT,
    DEFAULT_AGENT_MODELS,
    DEFAULT_INTERRUPT_TIMEOUT_SECONDS,
    DEFAULT_MCP_LIST_LIMIT,
    DEFAULT_MODEL_LIST_LIMIT,
    DEFAULT_PAGE_SIZE,
    DEFAULT_UPDATE_REPO_REF,
    DEFAULT_UPDATE_REPO_URL,
    INIT_PROMPT,
    MAX_MENTION_BYTES,
    MAX_TOPIC_THREAD_HISTORY,
    MODEL_PICKER_PROMPT,
    OPENCODE_TURN_TIMEOUT_SECONDS,
    PLACEHOLDER_TEXT,
    QUEUED_PLACEHOLDER_TEXT,
    RESUME_MISSING_IDS_LOG_LIMIT,
    RESUME_PICKER_PROMPT,
    RESUME_PREVIEW_ASSISTANT_LIMIT,
    RESUME_PREVIEW_USER_LIMIT,
    RESUME_REFRESH_LIMIT,
    REVIEW_COMMIT_PICKER_PROMPT,
    SHELL_MESSAGE_BUFFER_CHARS,
    TELEGRAM_MAX_MESSAGE_LENGTH,
    THREAD_LIST_MAX_PAGES,
    THREAD_LIST_PAGE_LIMIT,
    UPDATE_PICKER_PROMPT,
    UPDATE_TARGET_OPTIONS,
    VALID_AGENT_VALUES,
    VALID_REASONING_EFFORTS,
    WHISPER_TRANSCRIPT_DISCLAIMER,
    TurnKey,
)
from ..handlers import messages as message_handlers
from ..helpers import (
    _approval_age_seconds,
    _clear_pending_compact_seed,
    _clear_policy_overrides,
    _coerce_model_options,
    _coerce_thread_list,
    _compact_preview,
    _compose_agent_response,
    _compose_interrupt_response,
    _consume_raw_token,
    _extract_command_result,
    _extract_first_user_preview,
    _extract_rate_limits,
    _extract_rollout_path,
    _extract_thread_id,
    _extract_thread_info,
    _extract_thread_list_cursor,
    _extract_thread_preview_parts,
    _find_thread_entry,
    _format_feature_flags,
    _format_help_text,
    _format_mcp_list,
    _format_missing_thread_label,
    _format_model_list,
    _format_persist_note,
    _format_rate_limits,
    _format_resume_summary,
    _format_review_commit_label,
    _format_sandbox_policy,
    _format_shell_body,
    _format_skills_list,
    _format_thread_preview,
    _format_token_usage,
    _local_workspace_threads,
    _looks_binary,
    _normalize_approval_preset,
    _page_slice,
    _parse_review_commit_log,
    _partition_threads,
    _path_within,
    _paths_compatible,
    _prepare_shell_response,
    _preview_from_text,
    _render_command_output,
    _repo_root,
    _resume_thread_list_limit,
    _set_model_overrides,
    _set_pending_compact_seed,
    _set_policy_overrides,
    _set_rollout_path,
    _set_thread_summary,
    _split_topic_key,
    _thread_summary_preview,
    _with_conversation_id,
    find_github_links,
    is_interrupt_status,
)
from ..state import (
    APPROVAL_MODE_YOLO,
    PendingVoiceRecord,
    normalize_agent,
    parse_topic_key,
    topic_key,
)
from ..types import (
    CompactState,
    ModelPickerState,
    ReviewCommitSelectionState,
    SelectionState,
    TurnContext,
)

if TYPE_CHECKING:
    from ..state import TelegramTopicRecord


PROMPT_CONTEXT_RE = re.compile(r"\bprompt\b", re.IGNORECASE)
PROMPT_CONTEXT_HINT = (
    "If the user asks to write a prompt, put the prompt in a ```code block```."
)
OUTBOX_CONTEXT_RE = re.compile(
    r"(?:\b(?:pdf|png|jpg|jpeg|gif|webp|svg|csv|tsv|json|yaml|yml|zip|tar|"
    r"gz|tgz|xlsx|xls|docx|pptx|md|txt|log|html|xml)\b|"
    r"\.(?:pdf|png|jpg|jpeg|gif|webp|svg|csv|tsv|json|yaml|yml|zip|tar|"
    r"gz|tgz|xlsx|xls|docx|pptx|md|txt|log|html|xml)\b|"
    r"\b(?:outbox)\b)",
    re.IGNORECASE,
)
CAR_CONTEXT_KEYWORDS = (
    "car",
    "codex",
    "todo",
    "progress",
    "opinions",
    "spec",
    "summary",
    "autorunner",
    "work docs",
)
CAR_CONTEXT_HINT = (
    "Context: read .codex-autorunner/ABOUT_CAR.md for repo-specific rules."
)
FILES_HINT_TEMPLATE = (
    "Inbox: {inbox}\n"
    "Outbox (pending): {outbox}\n"
    "Topic key: {topic_key}\n"
    "Topic dir: {topic_dir}\n"
    "Place files in outbox pending to send after this turn finishes.\n"
    "Check delivery with /files outbox.\n"
    "Max file size: {max_bytes} bytes."
)


@dataclass
class _TurnRunResult:
    record: "TelegramTopicRecord"
    thread_id: Optional[str]
    turn_id: Optional[str]
    response: str
    placeholder_id: Optional[int]
    elapsed_seconds: Optional[float]
    token_usage: Optional[dict[str, Any]]
    transcript_message_id: Optional[int]
    transcript_text: Optional[str]


@dataclass
class _TurnRunFailure:
    failure_message: str
    placeholder_id: Optional[int]
    transcript_message_id: Optional[int]
    transcript_text: Optional[str]


@dataclass
class _RuntimeStub:
    current_turn_id: Optional[str] = None
    current_turn_key: Optional[TurnKey] = None
    interrupt_requested: bool = False
    interrupt_message_id: Optional[int] = None
    interrupt_turn_id: Optional[str] = None


def _extract_opencode_error_detail(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    error = payload.get("error")
    if isinstance(error, dict):
        for key in ("message", "detail", "error", "reason"):
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


def _format_opencode_exception(exc: Exception) -> Optional[str]:
    if isinstance(exc, OpenCodeSupervisorError):
        detail = str(exc).strip()
        if detail:
            return f"OpenCode backend unavailable ({detail})."
        return "OpenCode backend unavailable."
    if isinstance(exc, OpenCodeProtocolError):
        detail = str(exc).strip()
        if detail:
            return f"OpenCode protocol error: {detail}"
        return "OpenCode protocol error."
    if isinstance(exc, json.JSONDecodeError):
        return "OpenCode returned invalid JSON."
    if isinstance(exc, httpx.HTTPStatusError):
        detail = None
        try:
            detail = _extract_opencode_error_detail(exc.response.json())
        except Exception:
            detail = None
        if detail:
            return f"OpenCode error: {detail}"
        response_text = exc.response.text.strip()
        if response_text:
            return f"OpenCode error: {response_text}"
        return f"OpenCode request failed (HTTP {exc.response.status_code})."
    if isinstance(exc, httpx.RequestError):
        detail = str(exc).strip()
        if detail:
            return f"OpenCode request failed: {detail}"
        return "OpenCode request failed."
    return None


def _opencode_review_arguments(target: dict[str, Any]) -> str:
    target_type = target.get("type")
    if target_type == "uncommittedChanges":
        return ""
    if target_type == "baseBranch":
        branch = target.get("branch")
        if isinstance(branch, str) and branch:
            return branch
    if target_type == "commit":
        sha = target.get("sha")
        if isinstance(sha, str) and sha:
            return sha
    if target_type == "custom":
        instructions = target.get("instructions")
        if isinstance(instructions, str):
            instructions = instructions.strip()
            if instructions:
                return f"uncommitted\n\n{instructions}"
        return "uncommitted"
    return json.dumps(target, sort_keys=True)


def _coerce_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except Exception:
        return None


_OPENCODE_USAGE_TOTAL_KEYS = ("totalTokens", "total_tokens", "total")
_OPENCODE_USAGE_INPUT_KEYS = (
    "inputTokens",
    "input_tokens",
    "promptTokens",
    "prompt_tokens",
)
_OPENCODE_USAGE_CACHED_KEYS = (
    "cachedInputTokens",
    "cached_input_tokens",
    "cachedTokens",
    "cached_tokens",
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
    return usage or None


def _extract_opencode_usage_payload(payload: dict[str, Any]) -> dict[str, Any]:
    for key in (
        "usage",
        "tokenUsage",
        "token_usage",
        "usage_stats",
        "usageStats",
        "stats",
    ):
        usage = payload.get(key)
        if isinstance(usage, dict):
            return usage
    tokens = payload.get("tokens")
    if isinstance(tokens, dict):
        flattened = _flatten_opencode_tokens(tokens)
        if flattened:
            return flattened
    return payload


def _extract_opencode_usage_value(
    payload: dict[str, Any], keys: tuple[str, ...]
) -> Optional[int]:
    for key in keys:
        value = _coerce_int(payload.get(key))
        if value is not None:
            return value
    return None


def _build_opencode_token_usage(payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    usage_payload = _extract_opencode_usage_payload(payload)
    total_tokens = _extract_opencode_usage_value(
        usage_payload, _OPENCODE_USAGE_TOTAL_KEYS
    )
    input_tokens = _extract_opencode_usage_value(
        usage_payload, _OPENCODE_USAGE_INPUT_KEYS
    )
    cached_tokens = _extract_opencode_usage_value(
        usage_payload, _OPENCODE_USAGE_CACHED_KEYS
    )
    output_tokens = _extract_opencode_usage_value(
        usage_payload, _OPENCODE_USAGE_OUTPUT_KEYS
    )
    reasoning_tokens = _extract_opencode_usage_value(
        usage_payload, _OPENCODE_USAGE_REASONING_KEYS
    )
    if total_tokens is None:
        components = [
            value
            for value in (
                input_tokens,
                cached_tokens,
                output_tokens,
                reasoning_tokens,
            )
            if isinstance(value, int)
        ]
        if components:
            total_tokens = sum(components)
    if total_tokens is None:
        return None
    usage_line: dict[str, Any] = {"totalTokens": total_tokens}
    if input_tokens is not None:
        usage_line["inputTokens"] = input_tokens
    if cached_tokens is not None:
        usage_line["cachedInputTokens"] = cached_tokens
    if output_tokens is not None:
        usage_line["outputTokens"] = output_tokens
    if reasoning_tokens is not None:
        usage_line["reasoningTokens"] = reasoning_tokens
    token_usage: dict[str, Any] = {"last": usage_line}
    context_window = _extract_opencode_usage_value(
        payload, _OPENCODE_CONTEXT_WINDOW_KEYS
    )
    if context_window is None:
        context_window = _extract_opencode_usage_value(
            usage_payload, _OPENCODE_CONTEXT_WINDOW_KEYS
        )
    if context_window is not None and context_window > 0:
        token_usage["modelContextWindow"] = context_window
    return token_usage


def _format_httpx_exception(exc: Exception) -> Optional[str]:
    if isinstance(exc, httpx.HTTPStatusError):
        try:
            payload = exc.response.json()
        except Exception:
            payload = None
        if isinstance(payload, dict):
            detail = (
                payload.get("detail") or payload.get("message") or payload.get("error")
            )
            if isinstance(detail, str) and detail:
                return detail
        response_text = exc.response.text.strip()
        if response_text:
            return response_text
        return f"Request failed (HTTP {exc.response.status_code})."
    if isinstance(exc, httpx.RequestError):
        detail = str(exc).strip()
        if detail:
            return detail
        return "Request failed."
    return None


_GENERIC_TELEGRAM_ERRORS = {
    "Telegram request failed",
    "Telegram file download failed",
    "Telegram API returned error",
}


def _iter_exception_chain(exc: BaseException) -> list[BaseException]:
    chain: list[BaseException] = []
    current: Optional[BaseException] = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        chain.append(current)
        seen.add(id(current))
        current = current.__cause__ or current.__context__
    return chain


def _sanitize_error_detail(detail: str, *, limit: int = 200) -> str:
    cleaned = " ".join(detail.split())
    if len(cleaned) > limit:
        return f"{cleaned[: limit - 3]}..."
    return cleaned


def _format_telegram_download_error(exc: Exception) -> Optional[str]:
    for current in _iter_exception_chain(exc):
        if isinstance(current, Exception):
            detail = _format_httpx_exception(current)
            if detail:
                return _sanitize_error_detail(detail)
            message = str(current).strip()
            if message and message not in _GENERIC_TELEGRAM_ERRORS:
                return _sanitize_error_detail(message)
    return None


def _format_download_failure_response(kind: str, detail: Optional[str]) -> str:
    base = f"Failed to download {kind}."
    if detail:
        return f"{base} Reason: {detail}"
    return base


def _format_media_batch_failure(
    *,
    image_disabled: int,
    file_disabled: int,
    image_too_large: int,
    file_too_large: int,
    image_download_failed: int,
    file_download_failed: int,
    image_download_detail: Optional[str] = None,
    file_download_detail: Optional[str] = None,
    image_save_failed: int,
    file_save_failed: int,
    unsupported: int,
    max_image_bytes: int,
    max_file_bytes: int,
) -> str:
    base = "Failed to process any media in the batch."
    details: list[str] = []
    if image_disabled:
        details.append(f"{image_disabled} image(s) skipped (image handling disabled).")
    if file_disabled:
        details.append(f"{file_disabled} file(s) skipped (file handling disabled).")
    if image_too_large:
        details.append(
            f"{image_too_large} image(s) too large (max {max_image_bytes} bytes)."
        )
    if file_too_large:
        details.append(
            f"{file_too_large} file(s) too large (max {max_file_bytes} bytes)."
        )
    if image_download_failed:
        line = f"{image_download_failed} image(s) failed to download."
        if image_download_detail:
            label = "error" if image_download_failed == 1 else "last error"
            line = f"{line} ({label}: {image_download_detail})"
        details.append(line)
    if file_download_failed:
        line = f"{file_download_failed} file(s) failed to download."
        if file_download_detail:
            label = "error" if file_download_failed == 1 else "last error"
            line = f"{line} ({label}: {file_download_detail})"
        details.append(line)
    if image_save_failed:
        details.append(f"{image_save_failed} image(s) failed to save.")
    if file_save_failed:
        details.append(f"{file_save_failed} file(s) failed to save.")
    if unsupported:
        details.append(f"{unsupported} item(s) had unsupported media types.")
    if not details:
        return base
    return f"{base}\n" + "\n".join(f"- {line}" for line in details)


def _extract_opencode_session_path(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    for key in ("directory", "path", "workspace_path", "workspacePath"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    properties = payload.get("properties")
    if isinstance(properties, dict):
        for key in ("directory", "path", "workspace_path", "workspacePath"):
            value = properties.get(key)
            if isinstance(value, str) and value:
                return value
    session = payload.get("session")
    if isinstance(session, dict):
        return _extract_opencode_session_path(session)
    return None


class TelegramCommandHandlers:
    async def _handle_help(
        self, message: TelegramMessage, _args: str, _runtime: Any
    ) -> None:
        await self._send_message(
            message.chat_id,
            _format_help_text(self._command_specs),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _apply_agent_change(
        self,
        chat_id: int,
        thread_id: Optional[int],
        desired: str,
    ) -> str:
        def apply(record: "TelegramTopicRecord") -> None:
            record.agent = desired
            record.active_thread_id = None
            record.thread_ids.clear()
            record.thread_summaries.clear()
            record.pending_compact_seed = None
            record.pending_compact_seed_thread_id = None
            if not self._agent_supports_effort(desired):
                record.effort = None
            record.model = DEFAULT_AGENT_MODELS.get(desired)

        await self._router.update_topic(chat_id, thread_id, apply)
        if not self._agent_supports_resume(desired):
            return " (resume not supported)"
        return ""

    async def _handle_agent(
        self, message: TelegramMessage, args: str, _runtime: Any
    ) -> None:
        record = await self._router.ensure_topic(message.chat_id, message.thread_id)
        current = self._effective_agent(record)
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        self._agent_options.pop(key, None)
        argv = self._parse_command_args(args)
        if not argv:
            availability = "available"
            if not self._opencode_available():
                availability = "missing binary"
            items = []
            for agent in ("codex", "opencode"):
                if agent not in VALID_AGENT_VALUES:
                    continue
                label = agent
                if agent == current:
                    label = f"{label} (current)"
                if agent == "opencode" and availability != "available":
                    label = f"{label} ({availability})"
                items.append((agent, label))
            state = SelectionState(items=items)
            keyboard = self._build_agent_keyboard(state)
            self._agent_options[key] = state
            self._touch_cache_timestamp("agent_options", key)
            await self._send_message(
                message.chat_id,
                self._selection_prompt(AGENT_PICKER_PROMPT, state),
                thread_id=message.thread_id,
                reply_to=message.message_id,
                reply_markup=keyboard,
            )
            return
        desired = normalize_agent(argv[0])
        try:
            client = await self._client_for_workspace(record.workspace_path)
        except AppServerUnavailableError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.app_server.unavailable",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                "App server unavailable; try again or check logs.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if client is None:
            await self._send_message(
                message.chat_id,
                "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if desired == "opencode" and not self._opencode_available():
            await self._send_message(
                message.chat_id,
                "OpenCode binary not found. Install opencode or switch to /agent codex.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if desired == current:
            await self._send_message(
                message.chat_id,
                f"Agent already set to {current}.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        note = await self._apply_agent_change(
            message.chat_id, message.thread_id, desired
        )
        await self._send_message(
            message.chat_id,
            f"Agent set to {desired}{note}.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_command(
        self,
        command: TelegramCommand,
        message: TelegramMessage,
        runtime: Any,
    ) -> None:
        name = command.name
        args = command.args
        log_event(
            self._logger,
            logging.INFO,
            "telegram.command",
            name=name,
            args_len=len(args),
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
        )
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        spec = self._command_specs.get(name)
        if spec is None:
            self._resume_options.pop(key, None)
            self._bind_options.pop(key, None)
            self._agent_options.pop(key, None)
            self._model_options.pop(key, None)
            self._model_pending.pop(key, None)
            if name in ("list", "ls"):
                await self._send_message(
                    message.chat_id,
                    "Use /resume to list and switch threads.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            await self._send_message(
                message.chat_id,
                f"Unsupported command: /{name}. Send /help for options.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if runtime.current_turn_id and not spec.allow_during_turn:
            await self._send_message(
                message.chat_id,
                COMMAND_DISABLED_TEMPLATE.format(name=name),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        await spec.handler(message, args, runtime)

    def _parse_command_args(self, args: str) -> list[str]:
        if not args:
            return []
        try:
            return [part for part in shlex.split(args) if part]
        except ValueError:
            return [part for part in args.split() if part]

    def _effective_policies(
        self, record: "TelegramTopicRecord"
    ) -> tuple[Optional[str], Optional[Any]]:
        approval_policy, sandbox_policy = self._config.defaults.policies_for_mode(
            record.approval_mode
        )
        if record.approval_policy is not None:
            approval_policy = record.approval_policy
        if record.sandbox_policy is not None:
            sandbox_policy = record.sandbox_policy
        return approval_policy, sandbox_policy

    def _effective_agent(self, record: Optional["TelegramTopicRecord"]) -> str:
        if record and record.agent in VALID_AGENT_VALUES:
            return record.agent
        return DEFAULT_AGENT

    def _agent_supports_effort(self, agent: str) -> bool:
        return agent == "codex"

    def _agent_supports_resume(self, agent: str) -> bool:
        return agent in ("codex", "opencode")

    def _agent_rate_limit_source(self, agent: str) -> Optional[str]:
        if agent == "codex":
            return "app_server"
        return None

    def _opencode_available(self) -> bool:
        raw_command = getenv("CAR_OPENCODE_COMMAND")
        if resolve_opencode_binary(raw_command):
            return True
        binary = self._config.agent_binaries.get("opencode")
        if not binary:
            return False
        return resolve_opencode_binary(binary) is not None

    async def _resolve_opencode_model_context_window(
        self,
        opencode_client: Any,
        workspace_root: Path,
        model_payload: Optional[dict[str, str]],
    ) -> Optional[int]:
        if not model_payload:
            return None
        provider_id = model_payload.get("providerID")
        model_id = model_payload.get("modelID")
        if not provider_id or not model_id:
            return None
        cache: Optional[dict[str, dict[str, Optional[int]]]] = getattr(
            self, "_opencode_model_context_cache", None
        )
        if cache is None:
            cache = {}
            self._opencode_model_context_cache = cache
        workspace_key = str(workspace_root)
        workspace_cache = cache.setdefault(workspace_key, {})
        cache_key = f"{provider_id}/{model_id}"
        if cache_key in workspace_cache:
            return workspace_cache[cache_key]
        try:
            payload = await opencode_client.providers(directory=str(workspace_root))
        except Exception:
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
        context_window = None
        for provider in providers:
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
        workspace_cache[cache_key] = context_window
        return context_window

    async def _fetch_model_list(
        self,
        record: Optional["TelegramTopicRecord"],
        *,
        agent: str,
        client: CodexAppServerClient,
        list_params: dict[str, Any],
    ) -> Any:
        if agent == "opencode":
            supervisor = getattr(self, "_opencode_supervisor", None)
            if supervisor is None:
                raise OpenCodeSupervisorError("OpenCode backend is not configured")
            workspace_root = self._canonical_workspace_root(
                record.workspace_path if record else None
            )
            if workspace_root is None:
                raise OpenCodeSupervisorError("OpenCode workspace is unavailable")
            harness = OpenCodeHarness(supervisor)
            catalog = await harness.model_catalog(workspace_root)
            return [
                {
                    "id": model.id,
                    "displayName": model.display_name,
                }
                for model in catalog.models
            ]
        return await client.request("model/list", list_params)

    async def _verify_active_thread(
        self, message: TelegramMessage, record: "TelegramTopicRecord"
    ) -> Optional["TelegramTopicRecord"]:
        agent = self._effective_agent(record)
        if agent == "opencode":
            if not record.active_thread_id:
                return record
            supervisor = getattr(self, "_opencode_supervisor", None)
            if supervisor is None:
                await self._send_message(
                    message.chat_id,
                    "OpenCode backend unavailable; install opencode or switch to /agent codex.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return await self._router.set_active_thread(
                    message.chat_id, message.thread_id, None
                )
            workspace_root = self._canonical_workspace_root(record.workspace_path)
            if workspace_root is None:
                return record
            try:
                client = await supervisor.get_client(workspace_root)
                await client.get_session(record.active_thread_id)
                return record
            except Exception:
                return await self._router.set_active_thread(
                    message.chat_id, message.thread_id, None
                )
        if not self._agent_supports_resume(agent):
            return record
        thread_id = record.active_thread_id
        if not thread_id:
            return record
        try:
            client = await self._client_for_workspace(record.workspace_path)
        except AppServerUnavailableError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.app_server.unavailable",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                "App server unavailable; try again or check logs.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        if client is None:
            await self._send_message(
                message.chat_id,
                "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        try:
            result = await client.thread_resume(thread_id)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.thread.verify_failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                codex_thread_id=thread_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                "Failed to verify the active thread; use /resume or /new.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        info = _extract_thread_info(result)
        resumed_path = info.get("workspace_path")
        if not isinstance(resumed_path, str):
            await self._send_message(
                message.chat_id,
                "Active thread missing workspace metadata; refusing to continue. "
                "Fix the app-server workspace reporting and try /new.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return await self._router.set_active_thread(
                message.chat_id, message.thread_id, None
            )
        try:
            workspace_root = Path(record.workspace_path or "").expanduser().resolve()
            resumed_root = Path(resumed_path).expanduser().resolve()
        except Exception:
            await self._send_message(
                message.chat_id,
                "Active thread has invalid workspace metadata; refusing to continue. "
                "Fix the app-server workspace reporting and try /new.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return await self._router.set_active_thread(
                message.chat_id, message.thread_id, None
            )
        if not _paths_compatible(workspace_root, resumed_root):
            log_event(
                self._logger,
                logging.INFO,
                "telegram.thread.workspace_mismatch",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                codex_thread_id=thread_id,
                workspace_path=str(workspace_root),
                resumed_path=str(resumed_root),
            )
            await self._send_message(
                message.chat_id,
                "Active thread belongs to a different workspace; refusing to continue. "
                "Fix the app-server workspace reporting and try /new.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return await self._router.set_active_thread(
                message.chat_id, message.thread_id, None
            )
        return await self._apply_thread_result(
            message.chat_id, message.thread_id, result, active_thread_id=thread_id
        )

    async def _find_thread_conflict(self, thread_id: str, *, key: str) -> Optional[str]:
        return await self._store.find_active_thread(thread_id, exclude_key=key)

    async def _handle_thread_conflict(
        self,
        message: TelegramMessage,
        thread_id: str,
        conflict_key: str,
    ) -> None:
        log_event(
            self._logger,
            logging.WARNING,
            "telegram.thread.conflict",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            codex_thread_id=thread_id,
            conflict_topic=conflict_key,
        )
        await self._send_message(
            message.chat_id,
            "That Codex thread is already active in another topic. "
            "Use /new here or continue in the other topic.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _apply_thread_result(
        self,
        chat_id: int,
        thread_id: Optional[int],
        result: Any,
        *,
        active_thread_id: Optional[str] = None,
        overwrite_defaults: bool = False,
    ) -> "TelegramTopicRecord":
        info = _extract_thread_info(result)
        if active_thread_id is None:
            active_thread_id = info.get("thread_id")
        user_preview, assistant_preview = _extract_thread_preview_parts(result)
        last_used_at = now_iso()

        def apply(record: "TelegramTopicRecord") -> None:
            if active_thread_id:
                record.active_thread_id = active_thread_id
                if active_thread_id in record.thread_ids:
                    record.thread_ids.remove(active_thread_id)
                record.thread_ids.insert(0, active_thread_id)
                if len(record.thread_ids) > MAX_TOPIC_THREAD_HISTORY:
                    record.thread_ids = record.thread_ids[:MAX_TOPIC_THREAD_HISTORY]
                _set_thread_summary(
                    record,
                    active_thread_id,
                    user_preview=user_preview,
                    assistant_preview=assistant_preview,
                    last_used_at=last_used_at,
                    workspace_path=info.get("workspace_path"),
                    rollout_path=info.get("rollout_path"),
                )
            incoming_workspace = info.get("workspace_path")
            if isinstance(incoming_workspace, str) and incoming_workspace:
                if record.workspace_path:
                    try:
                        current_root = canonicalize_path(Path(record.workspace_path))
                        incoming_root = canonicalize_path(Path(incoming_workspace))
                    except Exception:
                        current_root = None
                        incoming_root = None
                    if (
                        current_root is None
                        or incoming_root is None
                        or not _paths_compatible(current_root, incoming_root)
                    ):
                        log_event(
                            self._logger,
                            logging.WARNING,
                            "telegram.workspace.mismatch",
                            workspace_path=record.workspace_path,
                            incoming_workspace_path=incoming_workspace,
                        )
                    else:
                        record.workspace_path = incoming_workspace
                else:
                    record.workspace_path = incoming_workspace
                record.workspace_id = self._workspace_id_for_path(record.workspace_path)
            if info.get("rollout_path"):
                record.rollout_path = info["rollout_path"]
            if info.get("agent") and (overwrite_defaults or record.agent is None):
                normalized_agent = normalize_agent(info.get("agent"))
                if normalized_agent:
                    record.agent = normalized_agent
            if info.get("model") and (overwrite_defaults or record.model is None):
                record.model = info["model"]
            if info.get("effort") and (overwrite_defaults or record.effort is None):
                record.effort = info["effort"]
            if info.get("summary") and (overwrite_defaults or record.summary is None):
                record.summary = info["summary"]
            allow_thread_policies = record.approval_mode != APPROVAL_MODE_YOLO
            if (
                allow_thread_policies
                and info.get("approval_policy")
                and (overwrite_defaults or record.approval_policy is None)
            ):
                record.approval_policy = info["approval_policy"]
            if (
                allow_thread_policies
                and info.get("sandbox_policy")
                and (overwrite_defaults or record.sandbox_policy is None)
            ):
                record.sandbox_policy = info["sandbox_policy"]

        return await self._router.update_topic(chat_id, thread_id, apply)

    async def _require_bound_record(
        self, message: TelegramMessage, *, prompt: Optional[str] = None
    ) -> Optional["TelegramTopicRecord"]:
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        record = await self._router.get_topic(key)
        if record is None or not record.workspace_path:
            await self._send_message(
                message.chat_id,
                prompt or "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        await self._refresh_workspace_id(key, record)
        return record

    async def _ensure_thread_id(
        self, message: TelegramMessage, record: "TelegramTopicRecord"
    ) -> Optional[str]:
        thread_id = record.active_thread_id
        if thread_id:
            key = await self._resolve_topic_key(message.chat_id, message.thread_id)
            conflict_key = await self._find_thread_conflict(thread_id, key=key)
            if conflict_key:
                await self._router.set_active_thread(
                    message.chat_id, message.thread_id, None
                )
                await self._handle_thread_conflict(message, thread_id, conflict_key)
                return None
            verified = await self._verify_active_thread(message, record)
            if not verified:
                return None
            record = verified
            thread_id = record.active_thread_id
            if thread_id:
                return thread_id
        agent = self._effective_agent(record)
        if agent == "opencode":
            supervisor = getattr(self, "_opencode_supervisor", None)
            if supervisor is None:
                await self._send_message(
                    message.chat_id,
                    "OpenCode backend unavailable; install opencode or switch to /agent codex.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return None
            workspace_root = self._canonical_workspace_root(record.workspace_path)
            if workspace_root is None:
                await self._send_message(
                    message.chat_id,
                    "Workspace unavailable.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return None
            try:
                opencode_client = await supervisor.get_client(workspace_root)
                session = await opencode_client.create_session(
                    directory=str(workspace_root)
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.opencode.session.failed",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    exc=exc,
                )
                await self._send_message(
                    message.chat_id,
                    "Failed to start a new OpenCode thread.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return None
            session_id = extract_session_id(session, allow_fallback_id=True)
            if not session_id:
                await self._send_message(
                    message.chat_id,
                    "Failed to start a new OpenCode thread.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return None

            def apply(record: "TelegramTopicRecord") -> None:
                record.active_thread_id = session_id
                if session_id in record.thread_ids:
                    record.thread_ids.remove(session_id)
                record.thread_ids.insert(0, session_id)
                if len(record.thread_ids) > MAX_TOPIC_THREAD_HISTORY:
                    record.thread_ids = record.thread_ids[:MAX_TOPIC_THREAD_HISTORY]
                _set_thread_summary(
                    record,
                    session_id,
                    last_used_at=now_iso(),
                    workspace_path=record.workspace_path,
                    rollout_path=record.rollout_path,
                )

            await self._router.update_topic(message.chat_id, message.thread_id, apply)
            return session_id
        try:
            client = await self._client_for_workspace(record.workspace_path)
        except AppServerUnavailableError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.app_server.unavailable",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                "App server unavailable; try again or check logs.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        if client is None:
            await self._send_message(
                message.chat_id,
                "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        thread = await client.thread_start(record.workspace_path or "", agent=agent)
        if not await self._require_thread_workspace(
            message, record.workspace_path, thread, action="thread_start"
        ):
            return None
        thread_id = _extract_thread_id(thread)
        if not thread_id:
            await self._send_message(
                message.chat_id,
                "Failed to start a new thread.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        await self._apply_thread_result(
            message.chat_id,
            message.thread_id,
            thread,
            active_thread_id=thread_id,
        )
        return thread_id

    def _list_manifest_repos(self) -> list[str]:
        if not self._manifest_path or not self._hub_root:
            return []
        try:
            manifest = load_manifest(self._manifest_path, self._hub_root)
        except Exception:
            return []
        repo_ids = [repo.id for repo in manifest.repos if repo.enabled]
        return repo_ids

    def _resolve_workspace(self, arg: str) -> Optional[tuple[str, Optional[str]]]:
        arg = (arg or "").strip()
        if not arg:
            return None
        if self._manifest_path and self._hub_root:
            try:
                manifest = load_manifest(self._manifest_path, self._hub_root)
                repo = manifest.get(arg)
                if repo:
                    workspace = canonicalize_path(self._hub_root / repo.path)
                    return str(workspace), repo.id
            except Exception:
                pass
        path = Path(arg)
        if not path.is_absolute():
            path = canonicalize_path(self._config.root / path)
        else:
            try:
                path = canonicalize_path(path)
            except Exception:
                return None
        if path.exists():
            return str(path), None
        return None

    async def _require_thread_workspace(
        self,
        message: TelegramMessage,
        expected_workspace: Optional[str],
        result: Any,
        *,
        action: str,
    ) -> bool:
        if not expected_workspace:
            return True
        info = _extract_thread_info(result)
        incoming = info.get("workspace_path")
        if not isinstance(incoming, str) or not incoming:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.thread.workspace_missing",
                action=action,
                expected_workspace=expected_workspace,
            )
            await self._send_message(
                message.chat_id,
                "App server did not return a workspace for this thread. "
                "Refusing to continue; fix the app-server workspace reporting and "
                "try /new.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return False
        try:
            expected_root = Path(expected_workspace).expanduser().resolve()
            incoming_root = Path(incoming).expanduser().resolve()
        except Exception:
            expected_root = None
            incoming_root = None
        if (
            expected_root is None
            or incoming_root is None
            or not _paths_compatible(expected_root, incoming_root)
        ):
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.thread.workspace_mismatch",
                action=action,
                expected_workspace=expected_workspace,
                incoming_workspace=incoming,
            )
            await self._send_message(
                message.chat_id,
                "App server returned a thread for a different workspace. "
                "Refusing to continue; fix the app-server workspace reporting and "
                "try /new.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return False
        return True

    async def _handle_normal_message(
        self,
        message: TelegramMessage,
        runtime: Any,
        *,
        text_override: Optional[str] = None,
        input_items: Optional[list[dict[str, Any]]] = None,
        record: Optional[TelegramTopicRecord] = None,
        send_placeholder: bool = True,
        transcript_message_id: Optional[int] = None,
        transcript_text: Optional[str] = None,
        placeholder_id: Optional[int] = None,
    ) -> None:
        if placeholder_id is not None:
            send_placeholder = False
        outcome = await self._run_turn_and_collect_result(
            message,
            runtime,
            text_override=text_override,
            input_items=input_items,
            record=record,
            send_placeholder=send_placeholder,
            transcript_message_id=transcript_message_id,
            transcript_text=transcript_text,
            allow_new_thread=True,
            send_failure_response=True,
            placeholder_id=placeholder_id,
        )
        if isinstance(outcome, _TurnRunFailure):
            return
        metrics = self._format_turn_metrics_text(
            outcome.token_usage, outcome.elapsed_seconds
        )
        metrics_mode = self._metrics_mode()
        response_text = outcome.response
        if metrics and metrics_mode == "append_to_response":
            response_text = f"{response_text}\n\n{metrics}"
        response_sent = await self._deliver_turn_response(
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            reply_to=message.message_id,
            placeholder_id=outcome.placeholder_id,
            response=response_text,
        )
        if response_sent:
            key = await self._resolve_topic_key(message.chat_id, message.thread_id)
            log_event(
                self._logger,
                logging.INFO,
                "telegram.response.sent",
                topic_key=key,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                placeholder_id=outcome.placeholder_id,
                final_response_sent_at=now_iso(),
            )
        placeholder_handled = False
        if metrics and metrics_mode == "separate":
            await self._send_turn_metrics(
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                reply_to=message.message_id,
                elapsed_seconds=outcome.elapsed_seconds,
                token_usage=outcome.token_usage,
            )
        elif metrics and metrics_mode == "append_to_progress" and response_sent:
            placeholder_handled = await self._append_metrics_to_placeholder(
                message.chat_id, outcome.placeholder_id, metrics
            )
        if outcome.turn_id:
            self._token_usage_by_turn.pop(outcome.turn_id, None)
        if response_sent:
            if not placeholder_handled:
                await self._delete_message(message.chat_id, outcome.placeholder_id)
            await self._finalize_voice_transcript(
                message.chat_id,
                outcome.transcript_message_id,
                outcome.transcript_text,
            )
        await self._flush_outbox_files(
            outcome.record,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    def _interrupt_keyboard(self) -> dict[str, Any]:
        return build_inline_keyboard(
            [[InlineButton("Cancel", encode_cancel_callback("interrupt"))]]
        )

    async def _await_turn_slot(
        self,
        turn_semaphore: asyncio.Semaphore,
        runtime: Any,
        *,
        message: TelegramMessage,
        placeholder_id: Optional[int],
        queued: bool,
    ) -> bool:
        cancel_event = asyncio.Event()
        runtime.queued_turn_cancel = cancel_event
        acquire_task = asyncio.create_task(turn_semaphore.acquire())
        cancel_task: Optional[asyncio.Task[bool]] = None
        try:
            if acquire_task.done():
                return True
            cancel_task = asyncio.create_task(cancel_event.wait())
            done, _ = await asyncio.wait(
                {acquire_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task in done and cancel_event.is_set():
                if acquire_task.done():
                    try:
                        turn_semaphore.release()
                    except ValueError:
                        pass
                if not acquire_task.done():
                    acquire_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await acquire_task
                if placeholder_id is not None:
                    await self._edit_message_text(
                        message.chat_id,
                        placeholder_id,
                        "Cancelled.",
                    )
                    await self._delete_message(message.chat_id, placeholder_id)
                return False
            if not acquire_task.done():
                await acquire_task
            return True
        finally:
            if cancel_task is not None and not cancel_task.done():
                cancel_task.cancel()
                with suppress(asyncio.CancelledError):
                    await cancel_task
            runtime.queued_turn_cancel = None

    async def _wait_for_turn_result(
        self,
        client: CodexAppServerClient,
        turn_handle: Any,
        *,
        timeout_seconds: Optional[float],
        topic_key: Optional[str],
        chat_id: int,
        thread_id: Optional[int],
    ) -> Any:
        if not timeout_seconds:
            return await turn_handle.wait()
        turn_task = asyncio.create_task(turn_handle.wait(timeout=None))
        timeout_task = asyncio.create_task(asyncio.sleep(timeout_seconds))
        try:
            done, _pending = await asyncio.wait(
                {turn_task, timeout_task}, return_when=asyncio.FIRST_COMPLETED
            )
            if turn_task in done:
                return await turn_task
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.turn.timeout",
                topic_key=topic_key,
                chat_id=chat_id,
                thread_id=thread_id,
                codex_thread_id=getattr(turn_handle, "thread_id", None),
                turn_id=getattr(turn_handle, "turn_id", None),
                timeout_seconds=timeout_seconds,
            )
            try:
                await client.turn_interrupt(
                    turn_handle.turn_id, thread_id=turn_handle.thread_id
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.turn.timeout_interrupt_failed",
                    topic_key=topic_key,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    codex_thread_id=getattr(turn_handle, "thread_id", None),
                    turn_id=getattr(turn_handle, "turn_id", None),
                    exc=exc,
                )
            done, _pending = await asyncio.wait(
                {turn_task}, timeout=DEFAULT_INTERRUPT_TIMEOUT_SECONDS
            )
            if not done:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.turn.timeout_grace_exhausted",
                    topic_key=topic_key,
                    chat_id=chat_id,
                    thread_id=thread_id,
                    codex_thread_id=getattr(turn_handle, "thread_id", None),
                    turn_id=getattr(turn_handle, "turn_id", None),
                )
                if not turn_task.done():
                    turn_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await turn_task
                raise asyncio.TimeoutError("Codex turn timed out")
            await turn_task
            raise asyncio.TimeoutError("Codex turn timed out")
        finally:
            timeout_task.cancel()
            with suppress(asyncio.CancelledError):
                await timeout_task

    async def _run_turn_and_collect_result(
        self,
        message: TelegramMessage,
        runtime: Any,
        *,
        text_override: Optional[str] = None,
        input_items: Optional[list[dict[str, Any]]] = None,
        record: Optional["TelegramTopicRecord"] = None,
        send_placeholder: bool = True,
        transcript_message_id: Optional[int] = None,
        transcript_text: Optional[str] = None,
        allow_new_thread: bool = True,
        missing_thread_message: Optional[str] = None,
        send_failure_response: bool = True,
        placeholder_id: Optional[int] = None,
    ) -> _TurnRunResult | _TurnRunFailure:
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        record = record or await self._router.get_topic(key)
        if record is None or not record.workspace_path:
            failure_message = "Topic not bound. Use /bind <repo_id> or /bind <path>."
            if send_failure_response:
                await self._send_message(
                    message.chat_id,
                    failure_message,
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
            return _TurnRunFailure(
                failure_message, None, transcript_message_id, transcript_text
            )
        turn_handle = None
        turn_started_at: Optional[float] = None
        turn_elapsed_seconds: Optional[float] = None
        if record.active_thread_id:
            conflict_key = await self._find_thread_conflict(
                record.active_thread_id,
                key=key,
            )
            if conflict_key:
                await self._router.set_active_thread(
                    message.chat_id, message.thread_id, None
                )
                await self._handle_thread_conflict(
                    message,
                    record.active_thread_id,
                    conflict_key,
                )
                return _TurnRunFailure(
                    "Thread conflict detected.",
                    placeholder_id,
                    transcript_message_id,
                    transcript_text,
                )
            verified = await self._verify_active_thread(message, record)
            if not verified:
                return _TurnRunFailure(
                    "Active thread verification failed.",
                    placeholder_id,
                    transcript_message_id,
                    transcript_text,
                )
            record = verified
        thread_id = record.active_thread_id
        prompt_text = (
            text_override if text_override is not None else (message.text or "")
        )
        prompt_text = self._maybe_append_whisper_disclaimer(
            prompt_text, transcript_text=transcript_text
        )
        prompt_text, injected = await self._maybe_inject_github_context(
            prompt_text, record
        )
        if injected and send_failure_response:
            await self._send_message(
                message.chat_id,
                "gh CLI used, github context injected",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
        prompt_text, injected = self._maybe_inject_car_context(prompt_text)
        if injected:
            log_event(
                self._logger,
                logging.INFO,
                "telegram.car_context.injected",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                message_id=message.message_id,
            )
        prompt_text, injected = self._maybe_inject_prompt_context(prompt_text)
        if injected:
            log_event(
                self._logger,
                logging.INFO,
                "telegram.prompt_context.injected",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                message_id=message.message_id,
            )
        prompt_text, injected = self._maybe_inject_outbox_context(
            prompt_text, record=record, topic_key=key
        )
        if injected:
            log_event(
                self._logger,
                logging.INFO,
                "telegram.outbox_context.injected",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                message_id=message.message_id,
            )
        turn_semaphore = self._ensure_turn_semaphore()
        queued = turn_semaphore.locked()
        placeholder_text = PLACEHOLDER_TEXT
        if queued:
            placeholder_text = QUEUED_PLACEHOLDER_TEXT
        if placeholder_id is None and send_placeholder:
            placeholder_id = await self._send_placeholder(
                message.chat_id,
                thread_id=message.thread_id,
                reply_to=message.message_id,
                text=placeholder_text,
            )
            log_event(
                self._logger,
                logging.INFO,
                "telegram.placeholder.sent",
                topic_key=key,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                placeholder_id=placeholder_id,
                placeholder_sent_at=now_iso(),
            )
        agent = self._effective_agent(record)
        if agent == "opencode":
            supervisor = getattr(self, "_opencode_supervisor", None)
            if supervisor is None:
                failure_message = "OpenCode backend unavailable; install opencode or switch to /agent codex."
                if send_failure_response:
                    await self._send_message(
                        message.chat_id,
                        failure_message,
                        thread_id=message.thread_id,
                        reply_to=message.message_id,
                    )
                return _TurnRunFailure(
                    failure_message,
                    placeholder_id,
                    transcript_message_id,
                    transcript_text,
                )
            workspace_root = self._canonical_workspace_root(record.workspace_path)
            if workspace_root is None:
                failure_message = "Workspace unavailable."
                if send_failure_response:
                    await self._send_message(
                        message.chat_id,
                        failure_message,
                        thread_id=message.thread_id,
                        reply_to=message.message_id,
                    )
                return _TurnRunFailure(
                    failure_message,
                    placeholder_id,
                    transcript_message_id,
                    transcript_text,
                )
            try:
                opencode_client = await supervisor.get_client(workspace_root)
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.opencode.client.failed",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    exc=exc,
                    error_at=now_iso(),
                    reason="opencode_client_failed",
                )
                failure_message = "OpenCode backend unavailable."
                if send_failure_response:
                    await self._send_message(
                        message.chat_id,
                        failure_message,
                        thread_id=message.thread_id,
                        reply_to=message.message_id,
                    )
                return _TurnRunFailure(
                    failure_message,
                    placeholder_id,
                    transcript_message_id,
                    transcript_text,
                )
            try:
                if not thread_id:
                    if not allow_new_thread:
                        failure_message = (
                            missing_thread_message
                            or "No active thread. Use /new to start one."
                        )
                        if send_failure_response:
                            await self._send_message(
                                message.chat_id,
                                failure_message,
                                thread_id=message.thread_id,
                                reply_to=message.message_id,
                            )
                        return _TurnRunFailure(
                            failure_message,
                            placeholder_id,
                            transcript_message_id,
                            transcript_text,
                        )
                    session = await opencode_client.create_session(
                        directory=str(workspace_root)
                    )
                    thread_id = extract_session_id(session, allow_fallback_id=True)
                    if not thread_id:
                        failure_message = "Failed to start a new OpenCode thread."
                        if send_failure_response:
                            await self._send_message(
                                message.chat_id,
                                failure_message,
                                thread_id=message.thread_id,
                                reply_to=message.message_id,
                            )
                        return _TurnRunFailure(
                            failure_message,
                            placeholder_id,
                            transcript_message_id,
                            transcript_text,
                        )

                    def apply(record: "TelegramTopicRecord") -> None:
                        record.active_thread_id = thread_id
                        if thread_id in record.thread_ids:
                            record.thread_ids.remove(thread_id)
                        record.thread_ids.insert(0, thread_id)
                        if len(record.thread_ids) > MAX_TOPIC_THREAD_HISTORY:
                            record.thread_ids = record.thread_ids[
                                :MAX_TOPIC_THREAD_HISTORY
                            ]
                        _set_thread_summary(
                            record,
                            thread_id,
                            last_used_at=now_iso(),
                            workspace_path=record.workspace_path,
                            rollout_path=record.rollout_path,
                        )

                    record = await self._router.update_topic(
                        message.chat_id, message.thread_id, apply
                    )
                else:
                    record = await self._router.set_active_thread(
                        message.chat_id, message.thread_id, thread_id
                    )
                user_preview = _preview_from_text(
                    prompt_text, RESUME_PREVIEW_USER_LIMIT
                )
                await self._router.update_topic(
                    message.chat_id,
                    message.thread_id,
                    lambda record: _set_thread_summary(
                        record,
                        thread_id,
                        user_preview=user_preview,
                        last_used_at=now_iso(),
                        workspace_path=record.workspace_path,
                        rollout_path=record.rollout_path,
                    ),
                )
                pending_seed = None
                pending_seed_thread_id = record.pending_compact_seed_thread_id
                if record.pending_compact_seed:
                    if pending_seed_thread_id is None:
                        pending_seed = record.pending_compact_seed
                    elif thread_id and pending_seed_thread_id == thread_id:
                        pending_seed = record.pending_compact_seed
                if pending_seed:
                    prompt_text = f"{pending_seed}\n\n{prompt_text}"
                queue_started_at = time.monotonic()
                log_event(
                    self._logger,
                    logging.INFO,
                    "telegram.turn.queued",
                    topic_key=key,
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    codex_thread_id=thread_id,
                    turn_queued_at=now_iso(),
                )
                acquired = await self._await_turn_slot(
                    turn_semaphore,
                    runtime,
                    message=message,
                    placeholder_id=placeholder_id,
                    queued=queued,
                )
                if not acquired:
                    runtime.interrupt_requested = False
                    return _TurnRunFailure(
                        "Cancelled.",
                        placeholder_id,
                        transcript_message_id,
                        transcript_text,
                    )
                turn_key: Optional[TurnKey] = None
                try:
                    queue_wait_ms = int((time.monotonic() - queue_started_at) * 1000)
                    log_event(
                        self._logger,
                        logging.INFO,
                        "telegram.turn.queue_wait",
                        topic_key=key,
                        chat_id=message.chat_id,
                        thread_id=message.thread_id,
                        codex_thread_id=thread_id,
                        queue_wait_ms=queue_wait_ms,
                        queued=queued,
                        max_parallel_turns=self._config.concurrency.max_parallel_turns,
                        per_topic_queue=self._config.concurrency.per_topic_queue,
                    )
                    if (
                        queued
                        and placeholder_id is not None
                        and placeholder_text != PLACEHOLDER_TEXT
                    ):
                        await self._edit_message_text(
                            message.chat_id,
                            placeholder_id,
                            PLACEHOLDER_TEXT,
                        )
                    opencode_turn_started = False
                    try:
                        await supervisor.mark_turn_started(workspace_root)
                        opencode_turn_started = True
                        model_payload = split_model_id(record.model)
                        missing_env = await opencode_missing_env(
                            opencode_client,
                            str(workspace_root),
                            model_payload,
                        )
                        if missing_env:
                            provider_id = (
                                model_payload.get("providerID")
                                if model_payload
                                else None
                            )
                            failure_message = (
                                "OpenCode provider "
                                f"{provider_id or 'selected'} requires env vars: "
                                f"{', '.join(missing_env)}. "
                                "Set them or switch models."
                            )
                            if send_failure_response:
                                await self._send_message(
                                    message.chat_id,
                                    failure_message,
                                    thread_id=message.thread_id,
                                    reply_to=message.message_id,
                                )
                            return _TurnRunFailure(
                                failure_message,
                                placeholder_id,
                                transcript_message_id,
                                transcript_text,
                            )
                        turn_started_at = time.monotonic()
                        log_event(
                            self._logger,
                            logging.INFO,
                            "telegram.turn.started",
                            topic_key=key,
                            chat_id=message.chat_id,
                            thread_id=message.thread_id,
                            codex_thread_id=thread_id,
                            turn_started_at=now_iso(),
                        )
                        turn_id = build_turn_id(thread_id)
                        if thread_id:
                            self._token_usage_by_thread.pop(thread_id, None)
                        runtime.current_turn_id = turn_id
                        runtime.current_turn_key = (thread_id, turn_id)
                        ctx = TurnContext(
                            topic_key=key,
                            chat_id=message.chat_id,
                            thread_id=message.thread_id,
                            codex_thread_id=thread_id,
                            reply_to_message_id=message.message_id,
                            placeholder_message_id=placeholder_id,
                        )
                        turn_key = self._turn_key(thread_id, turn_id)
                        if turn_key is None or not self._register_turn_context(
                            turn_key, turn_id, ctx
                        ):
                            runtime.current_turn_id = None
                            runtime.current_turn_key = None
                            runtime.interrupt_requested = False
                            failure_message = "Turn collision detected; please retry."
                            if send_failure_response:
                                await self._send_message(
                                    message.chat_id,
                                    failure_message,
                                    thread_id=message.thread_id,
                                    reply_to=message.message_id,
                                )
                                if placeholder_id is not None:
                                    await self._delete_message(
                                        message.chat_id, placeholder_id
                                    )
                            return _TurnRunFailure(
                                failure_message,
                                placeholder_id,
                                transcript_message_id,
                                transcript_text,
                            )
                        await self._start_turn_progress(
                            turn_key,
                            ctx=ctx,
                            agent="opencode",
                            model=record.model,
                            label="working",
                        )
                        approval_policy, _sandbox_policy = self._effective_policies(
                            record
                        )
                        permission_policy = map_approval_policy_to_permission(
                            approval_policy, default=PERMISSION_ALLOW
                        )

                        async def _permission_handler(
                            request_id: str, props: dict[str, Any]
                        ) -> str:
                            if permission_policy != PERMISSION_ASK:
                                return "reject"
                            prompt = format_permission_prompt(props)
                            decision = await self._handle_approval_request(
                                {
                                    "id": request_id,
                                    "method": "opencode/permission/requestApproval",
                                    "params": {
                                        "turnId": turn_id,
                                        "threadId": thread_id,
                                        "prompt": prompt,
                                    },
                                }
                            )
                            return decision

                        async def _question_handler(
                            request_id: str, props: dict[str, Any]
                        ) -> Optional[list[list[str]]]:
                            questions_raw = (
                                props.get("questions")
                                if isinstance(props, dict)
                                else None
                            )
                            questions = []
                            if isinstance(questions_raw, list):
                                questions = [
                                    question
                                    for question in questions_raw
                                    if isinstance(question, dict)
                                ]
                            return await self._handle_question_request(
                                request_id=request_id,
                                turn_id=turn_id,
                                thread_id=thread_id,
                                questions=questions,
                            )

                        abort_requested = False

                        async def _abort_opencode() -> None:
                            try:
                                await opencode_client.abort(thread_id)
                            except Exception:
                                pass

                        def _should_stop() -> bool:
                            nonlocal abort_requested
                            if runtime.interrupt_requested and not abort_requested:
                                abort_requested = True
                                asyncio.create_task(_abort_opencode())
                            return runtime.interrupt_requested

                        reasoning_buffers: dict[str, str] = {}
                        watched_session_ids = {thread_id}
                        subagent_labels: dict[str, str] = {}
                        opencode_context_window: Optional[int] = None
                        context_window_resolved = False

                        async def _handle_opencode_part(
                            part_type: str,
                            part: dict[str, Any],
                            delta_text: Optional[str],
                        ) -> None:
                            nonlocal opencode_context_window
                            nonlocal context_window_resolved
                            if turn_key is None:
                                return
                            tracker = self._turn_progress_trackers.get(turn_key)
                            if tracker is None:
                                return
                            session_id = None
                            for key in ("sessionID", "sessionId", "session_id"):
                                value = part.get(key)
                                if isinstance(value, str) and value:
                                    session_id = value
                                    break
                            if not session_id:
                                session_id = thread_id
                            is_primary_session = session_id == thread_id
                            subagent_label = subagent_labels.get(session_id)
                            if part_type == "reasoning":
                                part_id = (
                                    part.get("id") or part.get("partId") or "reasoning"
                                )
                                buffer_key = f"{session_id}:{part_id}"
                                buffer = reasoning_buffers.get(buffer_key, "")
                                if delta_text:
                                    buffer = f"{buffer}{delta_text}"
                                else:
                                    raw_text = part.get("text")
                                    if isinstance(raw_text, str) and raw_text:
                                        buffer = raw_text
                                if buffer:
                                    reasoning_buffers[buffer_key] = buffer
                                    preview = _compact_preview(buffer, limit=240)
                                    if is_primary_session:
                                        tracker.note_thinking(preview)
                                    else:
                                        if not subagent_label:
                                            subagent_label = "@subagent"
                                            subagent_labels.setdefault(
                                                session_id, subagent_label
                                            )
                                        if not tracker.update_action_by_item_id(
                                            buffer_key,
                                            preview,
                                            "update",
                                            label="thinking",
                                            subagent_label=subagent_label,
                                        ):
                                            tracker.add_action(
                                                "thinking",
                                                preview,
                                                "update",
                                                item_id=buffer_key,
                                                subagent_label=subagent_label,
                                            )
                            elif part_type == "tool":
                                tool_id = part.get("callID") or part.get("id")
                                tool_name = (
                                    part.get("tool") or part.get("name") or "tool"
                                )
                                status = None
                                state = part.get("state")
                                if isinstance(state, dict):
                                    status = state.get("status")
                                label = (
                                    f"{tool_name} ({status})"
                                    if isinstance(status, str) and status
                                    else str(tool_name)
                                )
                                if (
                                    is_primary_session
                                    and isinstance(tool_name, str)
                                    and tool_name == "task"
                                    and isinstance(state, dict)
                                ):
                                    metadata = state.get("metadata")
                                    if isinstance(metadata, dict):
                                        child_session_id = metadata.get(
                                            "sessionId"
                                        ) or metadata.get("sessionID")
                                        if (
                                            isinstance(child_session_id, str)
                                            and child_session_id
                                        ):
                                            watched_session_ids.add(child_session_id)
                                            child_label = None
                                            input_payload = state.get("input")
                                            if isinstance(input_payload, dict):
                                                child_label = input_payload.get(
                                                    "subagent_type"
                                                ) or input_payload.get("subagentType")
                                            if (
                                                isinstance(child_label, str)
                                                and child_label.strip()
                                            ):
                                                child_label = child_label.strip()
                                                if not child_label.startswith("@"):
                                                    child_label = f"@{child_label}"
                                                subagent_labels.setdefault(
                                                    child_session_id, child_label
                                                )
                                            else:
                                                subagent_labels.setdefault(
                                                    child_session_id, "@subagent"
                                                )
                                    detail_parts: list[str] = []
                                    title = state.get("title")
                                    if isinstance(title, str) and title.strip():
                                        detail_parts.append(title.strip())
                                    input_payload = state.get("input")
                                    if isinstance(input_payload, dict):
                                        description = input_payload.get("description")
                                        if (
                                            isinstance(description, str)
                                            and description.strip()
                                        ):
                                            detail_parts.append(description.strip())
                                    summary = None
                                    if isinstance(metadata, dict):
                                        summary = metadata.get("summary")
                                    if isinstance(summary, str) and summary.strip():
                                        detail_parts.append(summary.strip())
                                    if detail_parts:
                                        seen: set[str] = set()
                                        unique_parts = [
                                            part_text
                                            for part_text in detail_parts
                                            if part_text not in seen
                                            and not seen.add(part_text)
                                        ]
                                        detail_text = " / ".join(unique_parts)
                                        label = f"{label} - {_compact_preview(detail_text, limit=160)}"
                                mapped_status = "update"
                                if isinstance(status, str):
                                    status_lower = status.lower()
                                    if status_lower in ("completed", "done", "success"):
                                        mapped_status = "done"
                                    elif status_lower in ("error", "failed", "fail"):
                                        mapped_status = "fail"
                                    elif status_lower in ("pending", "running"):
                                        mapped_status = "running"
                                scoped_tool_id = (
                                    f"{session_id}:{tool_id}"
                                    if isinstance(tool_id, str) and tool_id
                                    else None
                                )
                                if is_primary_session:
                                    if not tracker.update_action_by_item_id(
                                        scoped_tool_id,
                                        label,
                                        mapped_status,
                                        label="tool",
                                    ):
                                        tracker.add_action(
                                            "tool",
                                            label,
                                            mapped_status,
                                            item_id=scoped_tool_id,
                                        )
                                else:
                                    if not subagent_label:
                                        subagent_label = "@subagent"
                                        subagent_labels.setdefault(
                                            session_id, subagent_label
                                        )
                                    if not tracker.update_action_by_item_id(
                                        scoped_tool_id,
                                        label,
                                        mapped_status,
                                        label=subagent_label,
                                    ):
                                        tracker.add_action(
                                            subagent_label,
                                            label,
                                            mapped_status,
                                            item_id=scoped_tool_id,
                                        )
                            elif part_type == "patch":
                                patch_id = part.get("id") or part.get("hash")
                                files = part.get("files")
                                scoped_patch_id = (
                                    f"{session_id}:{patch_id}"
                                    if isinstance(patch_id, str) and patch_id
                                    else None
                                )
                                if isinstance(files, list) and files:
                                    summary = ", ".join(str(file) for file in files)
                                    if not tracker.update_action_by_item_id(
                                        scoped_patch_id, summary, "done", label="files"
                                    ):
                                        tracker.add_action(
                                            "files",
                                            summary,
                                            "done",
                                            item_id=scoped_patch_id,
                                        )
                                else:
                                    if not tracker.update_action_by_item_id(
                                        scoped_patch_id, "Patch", "done", label="files"
                                    ):
                                        tracker.add_action(
                                            "files",
                                            "Patch",
                                            "done",
                                            item_id=scoped_patch_id,
                                        )
                            elif part_type == "agent":
                                agent_name = part.get("name") or "agent"
                                tracker.add_action("agent", str(agent_name), "done")
                            elif part_type == "step-start":
                                tracker.add_action("step", "started", "update")
                            elif part_type == "step-finish":
                                reason = part.get("reason") or "finished"
                                tracker.add_action("step", str(reason), "done")
                            elif part_type == "usage":
                                token_usage = (
                                    _build_opencode_token_usage(part)
                                    if isinstance(part, dict)
                                    else None
                                )
                                if token_usage:
                                    if is_primary_session:
                                        if (
                                            "modelContextWindow" not in token_usage
                                            and not context_window_resolved
                                        ):
                                            opencode_context_window = await self._resolve_opencode_model_context_window(
                                                opencode_client,
                                                workspace_root,
                                                model_payload,
                                            )
                                            context_window_resolved = True
                                        if (
                                            "modelContextWindow" not in token_usage
                                            and isinstance(opencode_context_window, int)
                                            and opencode_context_window > 0
                                        ):
                                            token_usage["modelContextWindow"] = (
                                                opencode_context_window
                                            )
                                        self._cache_token_usage(
                                            token_usage,
                                            turn_id=turn_id,
                                            thread_id=thread_id,
                                        )
                                        await self._note_progress_context_usage(
                                            token_usage,
                                            turn_id=turn_id,
                                            thread_id=thread_id,
                                        )
                            await self._schedule_progress_edit(turn_key)

                        ready_event = asyncio.Event()
                        output_task = asyncio.create_task(
                            collect_opencode_output(
                                opencode_client,
                                session_id=thread_id,
                                workspace_path=str(workspace_root),
                                progress_session_ids=watched_session_ids,
                                permission_policy=permission_policy,
                                permission_handler=(
                                    _permission_handler
                                    if permission_policy == PERMISSION_ASK
                                    else None
                                ),
                                question_handler=_question_handler,
                                should_stop=_should_stop,
                                part_handler=_handle_opencode_part,
                                ready_event=ready_event,
                            )
                        )
                        with suppress(asyncio.TimeoutError):
                            await asyncio.wait_for(ready_event.wait(), timeout=2.0)
                        prompt_task = asyncio.create_task(
                            opencode_client.prompt_async(
                                thread_id,
                                message=prompt_text,
                                model=model_payload,
                            )
                        )
                        try:
                            prompt_response = await prompt_task
                        except Exception as exc:
                            output_task.cancel()
                            with suppress(asyncio.CancelledError):
                                await output_task
                            raise exc
                        timeout_task = asyncio.create_task(
                            asyncio.sleep(OPENCODE_TURN_TIMEOUT_SECONDS)
                        )
                        done, _pending = await asyncio.wait(
                            {output_task, timeout_task},
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if timeout_task in done:
                            runtime.interrupt_requested = True
                            await _abort_opencode()
                            output_task.cancel()
                            with suppress(asyncio.CancelledError):
                                await output_task
                            turn_elapsed_seconds = time.monotonic() - turn_started_at
                            return _TurnRunFailure(
                                "OpenCode turn timed out.",
                                placeholder_id,
                                transcript_message_id,
                                transcript_text,
                            )
                        timeout_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await timeout_task
                        output_result = await output_task
                        if not output_result.text and not output_result.error:
                            fallback = parse_message_response(prompt_response)
                            if fallback.text:
                                output_result = OpenCodeTurnOutput(
                                    text=fallback.text, error=fallback.error
                                )
                        turn_elapsed_seconds = time.monotonic() - turn_started_at
                    finally:
                        if opencode_turn_started:
                            await supervisor.mark_turn_finished(workspace_root)
                finally:
                    turn_semaphore.release()
                if pending_seed:
                    await self._router.update_topic(
                        message.chat_id,
                        message.thread_id,
                        _clear_pending_compact_seed,
                    )
                output = output_result.text
                if output and prompt_text:
                    prompt_trimmed = prompt_text.strip()
                    output_trimmed = output.lstrip()
                    if prompt_trimmed and output_trimmed.startswith(prompt_trimmed):
                        output = output_trimmed[len(prompt_trimmed) :].lstrip()
                if output_result.error:
                    failure_message = f"OpenCode error: {output_result.error}"
                    if send_failure_response:
                        await self._send_message(
                            message.chat_id,
                            failure_message,
                            thread_id=message.thread_id,
                            reply_to=message.message_id,
                        )
                    return _TurnRunFailure(
                        failure_message,
                        placeholder_id,
                        transcript_message_id,
                        transcript_text,
                    )
                if output:
                    assistant_preview = _preview_from_text(
                        output, RESUME_PREVIEW_ASSISTANT_LIMIT
                    )
                    await self._router.update_topic(
                        message.chat_id,
                        message.thread_id,
                        lambda record: _set_thread_summary(
                            record,
                            thread_id,
                            assistant_preview=assistant_preview,
                            last_used_at=now_iso(),
                            workspace_path=record.workspace_path,
                            rollout_path=record.rollout_path,
                        ),
                    )
                token_usage = (
                    self._token_usage_by_turn.get(turn_id) if turn_id else None
                )
                return _TurnRunResult(
                    record=record,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    response=output or "No response.",
                    placeholder_id=placeholder_id,
                    elapsed_seconds=turn_elapsed_seconds,
                    token_usage=token_usage,
                    transcript_message_id=transcript_message_id,
                    transcript_text=transcript_text,
                )
            except Exception as exc:
                log_extra: dict[str, Any] = {}
                if isinstance(exc, httpx.HTTPStatusError):
                    log_extra["status_code"] = exc.response.status_code
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.opencode.turn.failed",
                    topic_key=key,
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    exc=exc,
                    **log_extra,
                    error_at=now_iso(),
                    reason="opencode_turn_failed",
                )
                failure_message = (
                    _format_opencode_exception(exc)
                    or "OpenCode turn failed; check logs for details."
                )
                if send_failure_response:
                    await self._send_message(
                        message.chat_id,
                        failure_message,
                        thread_id=message.thread_id,
                        reply_to=message.message_id,
                    )
                return _TurnRunFailure(
                    failure_message,
                    placeholder_id,
                    transcript_message_id,
                    transcript_text,
                )
            finally:
                if turn_key is not None:
                    self._turn_contexts.pop(turn_key, None)
                    self._clear_thinking_preview(turn_key)
                    self._clear_turn_progress(turn_key)
                if runtime.current_turn_key == turn_key:
                    runtime.current_turn_id = None
                    runtime.current_turn_key = None
                runtime.interrupt_requested = False
        try:
            client = await self._client_for_workspace(record.workspace_path)
        except AppServerUnavailableError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.app_server.unavailable",
                topic_key=key,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            failure_message = "App server unavailable; try again or check logs."
            if send_failure_response:
                await self._send_message(
                    message.chat_id,
                    failure_message,
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
            return _TurnRunFailure(
                failure_message, placeholder_id, transcript_message_id, transcript_text
            )
        if client is None:
            failure_message = "Topic not bound. Use /bind <repo_id> or /bind <path>."
            if send_failure_response:
                await self._send_message(
                    message.chat_id,
                    failure_message,
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
            return _TurnRunFailure(
                failure_message, None, transcript_message_id, transcript_text
            )
        try:
            if not thread_id:
                if not allow_new_thread:
                    failure_message = (
                        missing_thread_message
                        or "No active thread. Use /new to start one."
                    )
                    if send_failure_response:
                        await self._send_message(
                            message.chat_id,
                            failure_message,
                            thread_id=message.thread_id,
                            reply_to=message.message_id,
                        )
                    return _TurnRunFailure(
                        failure_message,
                        None,
                        transcript_message_id,
                        transcript_text,
                    )
                workspace_path = record.workspace_path
                if not workspace_path:
                    return _TurnRunFailure(
                        "Workspace missing.",
                        None,
                        transcript_message_id,
                        transcript_text,
                    )
                agent = self._effective_agent(record)
                thread = await client.thread_start(workspace_path, agent=agent)
                if not await self._require_thread_workspace(
                    message, workspace_path, thread, action="thread_start"
                ):
                    return _TurnRunFailure(
                        "Thread workspace mismatch.",
                        None,
                        transcript_message_id,
                        transcript_text,
                    )
                thread_id = _extract_thread_id(thread)
                if not thread_id:
                    failure_message = "Failed to start a new thread."
                    if send_failure_response:
                        await self._send_message(
                            message.chat_id,
                            failure_message,
                            thread_id=message.thread_id,
                            reply_to=message.message_id,
                        )
                    return _TurnRunFailure(
                        failure_message,
                        None,
                        transcript_message_id,
                        transcript_text,
                    )
                record = await self._apply_thread_result(
                    message.chat_id,
                    message.thread_id,
                    thread,
                    active_thread_id=thread_id,
                )
            else:
                record = await self._router.set_active_thread(
                    message.chat_id, message.thread_id, thread_id
                )
            if thread_id:
                user_preview = _preview_from_text(
                    prompt_text, RESUME_PREVIEW_USER_LIMIT
                )
                await self._router.update_topic(
                    message.chat_id,
                    message.thread_id,
                    lambda record: _set_thread_summary(
                        record,
                        thread_id,
                        user_preview=user_preview,
                        last_used_at=now_iso(),
                        workspace_path=record.workspace_path,
                        rollout_path=record.rollout_path,
                    ),
                )
            pending_seed = None
            pending_seed_thread_id = record.pending_compact_seed_thread_id
            if record.pending_compact_seed:
                if pending_seed_thread_id is None:
                    pending_seed = record.pending_compact_seed
                elif thread_id and pending_seed_thread_id == thread_id:
                    pending_seed = record.pending_compact_seed
            if pending_seed:
                if input_items is None:
                    input_items = [
                        {"type": "text", "text": pending_seed},
                        {"type": "text", "text": prompt_text},
                    ]
                else:
                    input_items = [{"type": "text", "text": pending_seed}] + input_items
            approval_policy, sandbox_policy = self._effective_policies(record)
            agent = self._effective_agent(record)
            supports_effort = self._agent_supports_effort(agent)
            turn_kwargs: dict[str, Any] = {}
            if agent:
                turn_kwargs["agent"] = agent
            if record.model:
                turn_kwargs["model"] = record.model
            if record.effort and supports_effort:
                turn_kwargs["effort"] = record.effort
            if record.summary:
                turn_kwargs["summary"] = record.summary
            log_event(
                self._logger,
                logging.INFO,
                "telegram.turn.starting",
                topic_key=key,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                codex_thread_id=thread_id,
                agent=agent,
                approval_mode=record.approval_mode,
                approval_policy=approval_policy,
                sandbox_policy=sandbox_policy,
            )

            queue_started_at = time.monotonic()
            log_event(
                self._logger,
                logging.INFO,
                "telegram.turn.queued",
                topic_key=key,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                codex_thread_id=thread_id,
                turn_queued_at=now_iso(),
            )
            acquired = await self._await_turn_slot(
                turn_semaphore,
                runtime,
                message=message,
                placeholder_id=placeholder_id,
                queued=queued,
            )
            if not acquired:
                runtime.interrupt_requested = False
                return _TurnRunFailure(
                    "Cancelled.",
                    placeholder_id,
                    transcript_message_id,
                    transcript_text,
                )
            turn_key: Optional[TurnKey] = None
            try:
                queue_wait_ms = int((time.monotonic() - queue_started_at) * 1000)
                log_event(
                    self._logger,
                    logging.INFO,
                    "telegram.turn.queue_wait",
                    topic_key=key,
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    codex_thread_id=thread_id,
                    queue_wait_ms=queue_wait_ms,
                    queued=queued,
                    max_parallel_turns=self._config.concurrency.max_parallel_turns,
                    per_topic_queue=self._config.concurrency.per_topic_queue,
                )
                if (
                    queued
                    and placeholder_id is not None
                    and placeholder_text != PLACEHOLDER_TEXT
                ):
                    await self._edit_message_text(
                        message.chat_id,
                        placeholder_id,
                        PLACEHOLDER_TEXT,
                    )
                turn_handle = await client.turn_start(
                    thread_id,
                    prompt_text,
                    input_items=input_items,
                    approval_policy=approval_policy,
                    sandbox_policy=sandbox_policy,
                    **turn_kwargs,
                )
                if pending_seed:
                    await self._router.update_topic(
                        message.chat_id,
                        message.thread_id,
                        _clear_pending_compact_seed,
                    )
                turn_started_at = time.monotonic()
                log_event(
                    self._logger,
                    logging.INFO,
                    "telegram.turn.started",
                    topic_key=key,
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    codex_thread_id=thread_id,
                    turn_started_at=now_iso(),
                )
                turn_key = self._turn_key(thread_id, turn_handle.turn_id)
                runtime.current_turn_id = turn_handle.turn_id
                runtime.current_turn_key = turn_key
                ctx = TurnContext(
                    topic_key=key,
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    codex_thread_id=thread_id,
                    reply_to_message_id=message.message_id,
                    placeholder_message_id=placeholder_id,
                )
                if turn_key is None or not self._register_turn_context(
                    turn_key, turn_handle.turn_id, ctx
                ):
                    runtime.current_turn_id = None
                    runtime.current_turn_key = None
                    runtime.interrupt_requested = False
                    failure_message = "Turn collision detected; please retry."
                    if send_failure_response:
                        await self._send_message(
                            message.chat_id,
                            failure_message,
                            thread_id=message.thread_id,
                            reply_to=message.message_id,
                        )
                        if placeholder_id is not None:
                            await self._delete_message(message.chat_id, placeholder_id)
                    return _TurnRunFailure(
                        failure_message,
                        placeholder_id,
                        transcript_message_id,
                        transcript_text,
                    )
                await self._start_turn_progress(
                    turn_key,
                    ctx=ctx,
                    agent=self._effective_agent(record),
                    model=record.model,
                    label="working",
                )
                result = await self._wait_for_turn_result(
                    client,
                    turn_handle,
                    timeout_seconds=self._config.app_server_turn_timeout_seconds,
                    topic_key=key,
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                )
                if turn_started_at is not None:
                    turn_elapsed_seconds = time.monotonic() - turn_started_at
            finally:
                turn_semaphore.release()
        except Exception as exc:
            if turn_handle is not None:
                if turn_key is not None:
                    self._turn_contexts.pop(turn_key, None)
            runtime.current_turn_id = None
            runtime.current_turn_key = None
            runtime.interrupt_requested = False
            failure_message = "Codex turn failed; check logs for details."
            reason = "codex_turn_failed"
            if isinstance(exc, asyncio.TimeoutError):
                failure_message = (
                    "Codex turn timed out; interrupting now. "
                    "Please resend your message in a moment."
                )
                reason = "turn_timeout"
            elif isinstance(exc, CodexAppServerDisconnected):
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.app_server.disconnected_during_turn",
                    topic_key=key,
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    turn_id=turn_handle.turn_id if turn_handle else None,
                )
                failure_message = (
                    "Codex app-server disconnected; recovering now. "
                    "Your request did not complete. Please resend your message in a moment."
                )
                reason = "app_server_disconnected"
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.turn.failed",
                topic_key=key,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
                error_at=now_iso(),
                reason=reason,
            )
            if send_failure_response:
                response_sent = await self._deliver_turn_response(
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                    placeholder_id=placeholder_id,
                    response=_with_conversation_id(
                        failure_message,
                        chat_id=message.chat_id,
                        thread_id=message.thread_id,
                    ),
                )
                if response_sent:
                    await self._delete_message(message.chat_id, placeholder_id)
                    await self._finalize_voice_transcript(
                        message.chat_id,
                        transcript_message_id,
                        transcript_text,
                    )
            return _TurnRunFailure(
                failure_message,
                placeholder_id,
                transcript_message_id,
                transcript_text,
            )
        finally:
            if turn_handle is not None:
                if turn_key is not None:
                    self._turn_contexts.pop(turn_key, None)
                    self._clear_thinking_preview(turn_key)
                    self._clear_turn_progress(turn_key)
            runtime.current_turn_id = None
            runtime.current_turn_key = None
            runtime.interrupt_requested = False

        response = _compose_agent_response(
            result.agent_messages, errors=result.errors, status=result.status
        )
        if thread_id and result.agent_messages:
            assistant_preview = _preview_from_text(
                response, RESUME_PREVIEW_ASSISTANT_LIMIT
            )
            if assistant_preview:
                await self._router.update_topic(
                    message.chat_id,
                    message.thread_id,
                    lambda record: _set_thread_summary(
                        record,
                        thread_id,
                        assistant_preview=assistant_preview,
                        last_used_at=now_iso(),
                        workspace_path=record.workspace_path,
                        rollout_path=record.rollout_path,
                    ),
                )
        turn_handle_id = turn_handle.turn_id if turn_handle else None
        if is_interrupt_status(result.status):
            response = _compose_interrupt_response(response)
            if (
                runtime.interrupt_message_id is not None
                and runtime.interrupt_turn_id == turn_handle_id
            ):
                await self._edit_message_text(
                    message.chat_id,
                    runtime.interrupt_message_id,
                    "Interrupted.",
                )
                runtime.interrupt_message_id = None
                runtime.interrupt_turn_id = None
            runtime.interrupt_requested = False
        elif runtime.interrupt_turn_id == turn_handle_id:
            if runtime.interrupt_message_id is not None:
                await self._edit_message_text(
                    message.chat_id,
                    runtime.interrupt_message_id,
                    "Interrupt requested; turn completed.",
                )
            runtime.interrupt_message_id = None
            runtime.interrupt_turn_id = None
            runtime.interrupt_requested = False
        log_event(
            self._logger,
            logging.INFO,
            "telegram.turn.completed",
            topic_key=key,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            turn_id=turn_handle.turn_id if turn_handle else None,
            status=result.status,
            agent_message_count=len(result.agent_messages),
            error_count=len(result.errors),
        )
        turn_id = turn_handle.turn_id if turn_handle else None
        token_usage = self._token_usage_by_turn.get(turn_id) if turn_id else None
        return _TurnRunResult(
            record=record,
            thread_id=thread_id,
            turn_id=turn_id,
            response=response,
            placeholder_id=placeholder_id,
            elapsed_seconds=turn_elapsed_seconds,
            token_usage=token_usage,
            transcript_message_id=transcript_message_id,
            transcript_text=transcript_text,
        )

    def _maybe_append_whisper_disclaimer(
        self,
        prompt_text: str,
        *,
        transcript_text: Optional[str],
    ) -> str:
        if not transcript_text:
            return prompt_text
        if WHISPER_TRANSCRIPT_DISCLAIMER in prompt_text:
            return prompt_text
        provider = None
        if self._voice_config is not None:
            provider = self._voice_config.provider
        provider = provider or "openai_whisper"
        if provider != "openai_whisper":
            return prompt_text
        disclaimer = wrap_injected_context(WHISPER_TRANSCRIPT_DISCLAIMER)
        if prompt_text.strip():
            return f"{prompt_text}\n\n{disclaimer}"
        return disclaimer

    async def _maybe_inject_github_context(
        self, prompt_text: str, record: Any
    ) -> tuple[str, bool]:
        if not prompt_text or not record or not record.workspace_path:
            return prompt_text, False
        links = find_github_links(prompt_text)
        if not links:
            log_event(
                self._logger,
                logging.DEBUG,
                "telegram.github_context.skip",
                reason="no_links",
            )
            return prompt_text, False
        workspace_root = Path(record.workspace_path)
        repo_root = _repo_root(workspace_root)
        if repo_root is None:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.github_context.skip",
                reason="repo_not_found",
                workspace_path=str(workspace_root),
            )
            return prompt_text, False
        try:
            repo_config = load_repo_config(repo_root)
            raw_config = repo_config.raw if repo_config else None
        except Exception:
            raw_config = None
        svc = GitHubService(repo_root, raw_config=raw_config)
        if not svc.gh_available():
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.github_context.skip",
                reason="gh_unavailable",
                repo_root=str(repo_root),
            )
            return prompt_text, False
        if not svc.gh_authenticated():
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.github_context.skip",
                reason="gh_unauthenticated",
                repo_root=str(repo_root),
            )
            return prompt_text, False
        for link in links:
            try:
                result = await asyncio.to_thread(svc.build_context_file_from_url, link)
            except Exception:
                result = None
            if result and result.get("hint"):
                separator = "\n" if prompt_text.endswith("\n") else "\n\n"
                hint = str(result["hint"])
                log_event(
                    self._logger,
                    logging.INFO,
                    "telegram.github_context.injected",
                    repo_root=str(repo_root),
                    path=result.get("path"),
                )
                return f"{prompt_text}{separator}{hint}", True
        log_event(
            self._logger,
            logging.INFO,
            "telegram.github_context.skip",
            reason="no_context",
            repo_root=str(repo_root),
        )
        return prompt_text, False

    def _maybe_inject_prompt_context(self, prompt_text: str) -> tuple[str, bool]:
        if not prompt_text or not prompt_text.strip():
            return prompt_text, False
        if PROMPT_CONTEXT_HINT in prompt_text:
            return prompt_text, False
        if not PROMPT_CONTEXT_RE.search(prompt_text):
            return prompt_text, False
        separator = "\n" if prompt_text.endswith("\n") else "\n\n"
        injection = wrap_injected_context(PROMPT_CONTEXT_HINT)
        return f"{prompt_text}{separator}{injection}", True

    def _maybe_inject_car_context(self, prompt_text: str) -> tuple[str, bool]:
        if not prompt_text or not prompt_text.strip():
            return prompt_text, False
        lowered = prompt_text.lower()
        if "about_car.md" in lowered:
            return prompt_text, False
        if CAR_CONTEXT_HINT in prompt_text:
            return prompt_text, False
        if not any(keyword in lowered for keyword in CAR_CONTEXT_KEYWORDS):
            return prompt_text, False
        separator = "\n" if prompt_text.endswith("\n") else "\n\n"
        injection = wrap_injected_context(CAR_CONTEXT_HINT)
        return f"{prompt_text}{separator}{injection}", True

    def _maybe_inject_outbox_context(
        self,
        prompt_text: str,
        *,
        record: "TelegramTopicRecord",
        topic_key: str,
    ) -> tuple[str, bool]:
        if not prompt_text or not prompt_text.strip():
            return prompt_text, False
        if "Outbox (pending):" in prompt_text or "Inbox:" in prompt_text:
            return prompt_text, False
        if not OUTBOX_CONTEXT_RE.search(prompt_text):
            return prompt_text, False
        inbox_dir = self._files_inbox_dir(record.workspace_path, topic_key)
        outbox_dir = self._files_outbox_pending_dir(record.workspace_path, topic_key)
        topic_dir = self._files_topic_dir(record.workspace_path, topic_key)
        separator = "\n" if prompt_text.endswith("\n") else "\n\n"
        injection = wrap_injected_context(
            FILES_HINT_TEMPLATE.format(
                inbox=str(inbox_dir),
                outbox=str(outbox_dir),
                topic_key=topic_key,
                topic_dir=str(topic_dir),
                max_bytes=self._config.media.max_file_bytes,
            )
        )
        return f"{prompt_text}{separator}{injection}", True

    async def _handle_image_message(
        self,
        message: TelegramMessage,
        runtime: Any,
        record: Any,
        candidate: TelegramMediaCandidate,
        caption_text: str,
        *,
        placeholder_id: Optional[int] = None,
    ) -> None:
        log_event(
            self._logger,
            logging.INFO,
            "telegram.media.image.received",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
            file_id=candidate.file_id,
            file_size=candidate.file_size,
            has_caption=bool(caption_text),
        )
        max_bytes = self._config.media.max_image_bytes
        if candidate.file_size and candidate.file_size > max_bytes:
            await self._send_message(
                message.chat_id,
                f"Image too large (max {max_bytes} bytes).",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        try:
            data, file_path, file_size = await self._download_telegram_file(
                candidate.file_id,
                max_bytes=max_bytes,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            detail = _format_telegram_download_error(exc)
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.media.image.download_failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                message_id=message.message_id,
                detail=detail,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                _format_download_failure_response("image", detail),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if file_size and file_size > max_bytes:
            await self._send_message(
                message.chat_id,
                f"Image too large (max {max_bytes} bytes).",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if len(data) > max_bytes:
            await self._send_message(
                message.chat_id,
                f"Image too large (max {max_bytes} bytes).",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        try:
            image_path = self._save_image_file(
                record.workspace_path, data, file_path, candidate
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.media.image.save_failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                message_id=message.message_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                "Failed to save image.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        prompt_text = caption_text.strip()
        if not prompt_text:
            prompt_text = self._config.media.image_prompt
        input_items = [
            {"type": "text", "text": prompt_text},
            {"type": "localImage", "path": str(image_path)},
        ]
        log_event(
            self._logger,
            logging.INFO,
            "telegram.media.image.ready",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
            path=str(image_path),
            prompt_len=len(prompt_text),
        )
        await self._handle_normal_message(
            message,
            runtime,
            text_override=prompt_text,
            input_items=input_items,
            record=record,
            placeholder_id=placeholder_id,
        )

    async def _handle_voice_message(
        self,
        message: TelegramMessage,
        runtime: Any,
        record: Any,
        candidate: TelegramMediaCandidate,
        caption_text: str,
        *,
        placeholder_id: Optional[int] = None,
    ) -> None:
        log_event(
            self._logger,
            logging.INFO,
            "telegram.media.voice.received",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
            file_id=candidate.file_id,
            file_size=candidate.file_size,
            duration=candidate.duration,
            has_caption=bool(caption_text),
        )
        if (
            not self._voice_service
            or not self._voice_config
            or not self._voice_config.enabled
        ):
            await self._send_message(
                message.chat_id,
                "Voice transcription is disabled.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        max_bytes = self._config.media.max_voice_bytes
        if candidate.file_size and candidate.file_size > max_bytes:
            await self._send_message(
                message.chat_id,
                f"Voice note too large (max {max_bytes} bytes).",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        pending = PendingVoiceRecord(
            record_id=secrets.token_hex(8),
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
            file_id=candidate.file_id,
            file_name=candidate.file_name,
            caption=caption_text,
            file_size=candidate.file_size,
            mime_type=candidate.mime_type,
            duration=candidate.duration,
            workspace_path=record.workspace_path,
            created_at=now_iso(),
        )
        await self._store.enqueue_pending_voice(pending)
        log_event(
            self._logger,
            logging.INFO,
            "telegram.media.voice.queued",
            record_id=pending.record_id,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
            file_id=candidate.file_id,
        )
        self._spawn_task(self._voice_manager.attempt(pending.record_id))

    async def _handle_file_message(
        self,
        message: TelegramMessage,
        runtime: Any,
        record: Any,
        candidate: TelegramMediaCandidate,
        caption_text: str,
        *,
        placeholder_id: Optional[int] = None,
    ) -> None:
        log_event(
            self._logger,
            logging.INFO,
            "telegram.media.file.received",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
            file_id=candidate.file_id,
            file_size=candidate.file_size,
            has_caption=bool(caption_text),
        )
        max_bytes = self._config.media.max_file_bytes
        if candidate.file_size and candidate.file_size > max_bytes:
            await self._send_message(
                message.chat_id,
                f"File too large (max {max_bytes} bytes).",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        try:
            data, file_path, file_size = await self._download_telegram_file(
                candidate.file_id,
                max_bytes=max_bytes,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            detail = _format_telegram_download_error(exc)
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.media.file.download_failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                message_id=message.message_id,
                detail=detail,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                _format_download_failure_response("file", detail),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if file_size and file_size > max_bytes:
            await self._send_message(
                message.chat_id,
                f"File too large (max {max_bytes} bytes).",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if len(data) > max_bytes:
            await self._send_message(
                message.chat_id,
                f"File too large (max {max_bytes} bytes).",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        try:
            file_path_local = self._save_inbox_file(
                record.workspace_path,
                key,
                data,
                candidate=candidate,
                file_path=file_path,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.media.file.save_failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                message_id=message.message_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                "Failed to save file.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        prompt_text = self._format_file_prompt(
            caption_text,
            candidate=candidate,
            saved_path=file_path_local,
            source_path=file_path,
            file_size=file_size or len(data),
            topic_key=key,
            workspace_path=record.workspace_path,
        )
        log_event(
            self._logger,
            logging.INFO,
            "telegram.media.file.ready",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
            path=str(file_path_local),
        )
        await self._handle_normal_message(
            message,
            runtime,
            text_override=prompt_text,
            record=record,
            placeholder_id=placeholder_id,
        )

    async def _handle_media_batch(
        self,
        messages: Sequence[TelegramMessage],
        *,
        placeholder_id: Optional[int] = None,
    ) -> None:
        if not messages:
            return
        if not self._config.media.enabled:
            first_msg = messages[0]
            await self._send_message(
                first_msg.chat_id,
                "Media handling is disabled.",
                thread_id=first_msg.thread_id,
                reply_to=first_msg.message_id,
            )
            return
        first_msg = messages[0]
        topic_key = await self._resolve_topic_key(
            first_msg.chat_id, first_msg.thread_id
        )
        record = await self._router.get_topic(topic_key)
        if record is None or not record.workspace_path:
            await self._send_message(
                first_msg.chat_id,
                self._with_conversation_id(
                    "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                    chat_id=first_msg.chat_id,
                    thread_id=first_msg.thread_id,
                ),
                thread_id=first_msg.thread_id,
                reply_to=first_msg.message_id,
            )
            return
        runtime = self._router.runtime_for(topic_key)

        sorted_messages = sorted(messages, key=lambda m: m.message_id)
        saved_image_paths: list[Path] = []
        saved_file_info: list[tuple[str, str, int]] = []
        failed_count = 0
        max_image_bytes = self._config.media.max_image_bytes
        max_file_bytes = self._config.media.max_file_bytes
        image_disabled = 0
        file_disabled = 0
        image_too_large = 0
        file_too_large = 0
        image_download_failed = 0
        file_download_failed = 0
        image_download_detail: Optional[str] = None
        file_download_detail: Optional[str] = None
        image_save_failed = 0
        file_save_failed = 0
        unsupported_count = 0

        for msg in sorted_messages:
            image_candidate = message_handlers.select_image_candidate(msg)
            file_candidate = message_handlers.select_file_candidate(msg)
            if not image_candidate and not file_candidate:
                unsupported_count += 1
                failed_count += 1
                continue
            if image_candidate:
                if not self._config.media.images:
                    await self._send_message(
                        msg.chat_id,
                        "Image handling is disabled.",
                        thread_id=msg.thread_id,
                        reply_to=msg.message_id,
                    )
                    image_disabled += 1
                    failed_count += 1
                    continue
                try:
                    data, file_path, file_size = await self._download_telegram_file(
                        image_candidate.file_id,
                        max_bytes=max_image_bytes,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    detail = _format_telegram_download_error(exc)
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "telegram.media_batch.image.download_failed",
                        chat_id=msg.chat_id,
                        thread_id=msg.thread_id,
                        message_id=msg.message_id,
                        detail=detail,
                        exc=exc,
                    )
                    if detail and image_download_detail is None:
                        image_download_detail = detail
                    image_download_failed += 1
                    failed_count += 1
                    continue
                if file_size and file_size > max_image_bytes:
                    await self._send_message(
                        msg.chat_id,
                        f"Image too large (max {max_image_bytes} bytes).",
                        thread_id=msg.thread_id,
                        reply_to=msg.message_id,
                    )
                    image_too_large += 1
                    failed_count += 1
                    continue
                if len(data) > max_image_bytes:
                    await self._send_message(
                        msg.chat_id,
                        f"Image too large (max {max_image_bytes} bytes).",
                        thread_id=msg.thread_id,
                        reply_to=msg.message_id,
                    )
                    image_too_large += 1
                    failed_count += 1
                    continue
                try:
                    image_path = self._save_image_file(
                        record.workspace_path, data, file_path, image_candidate
                    )
                    saved_image_paths.append(image_path)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "telegram.media_batch.image.save_failed",
                        chat_id=msg.chat_id,
                        thread_id=msg.thread_id,
                        message_id=msg.message_id,
                        exc=exc,
                    )
                    image_save_failed += 1
                    failed_count += 1
                    continue

            if file_candidate:
                if not self._config.media.files:
                    await self._send_message(
                        msg.chat_id,
                        "File handling is disabled.",
                        thread_id=msg.thread_id,
                        reply_to=msg.message_id,
                    )
                    file_disabled += 1
                    failed_count += 1
                    continue
                try:
                    data, file_path, file_size = await self._download_telegram_file(
                        file_candidate.file_id,
                        max_bytes=max_file_bytes,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    detail = _format_telegram_download_error(exc)
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "telegram.media_batch.file.download_failed",
                        chat_id=msg.chat_id,
                        thread_id=msg.thread_id,
                        message_id=msg.message_id,
                        detail=detail,
                        exc=exc,
                    )
                    if detail and file_download_detail is None:
                        file_download_detail = detail
                    file_download_failed += 1
                    failed_count += 1
                    continue
                if file_size is not None and file_size > max_file_bytes:
                    await self._send_message(
                        msg.chat_id,
                        f"File too large (max {max_file_bytes} bytes).",
                        thread_id=msg.thread_id,
                        reply_to=msg.message_id,
                    )
                    file_too_large += 1
                    failed_count += 1
                    continue
                if len(data) > max_file_bytes:
                    await self._send_message(
                        msg.chat_id,
                        f"File too large (max {max_file_bytes} bytes).",
                        thread_id=msg.thread_id,
                        reply_to=msg.message_id,
                    )
                    file_too_large += 1
                    failed_count += 1
                    continue
                try:
                    file_path_local = self._save_inbox_file(
                        record.workspace_path,
                        topic_key,
                        data,
                        candidate=file_candidate,
                        file_path=file_path,
                    )
                    original_name = (
                        file_candidate.file_name
                        or (Path(file_path).name if file_path else None)
                        or "unknown"
                    )
                    saved_file_info.append(
                        (
                            original_name,
                            str(file_path_local),
                            file_size or len(data),
                        )
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "telegram.media_batch.file.save_failed",
                        chat_id=msg.chat_id,
                        thread_id=msg.thread_id,
                        message_id=msg.message_id,
                        exc=exc,
                    )
                    file_save_failed += 1
                    failed_count += 1
                    continue

        if not saved_image_paths and not saved_file_info:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.media_batch.empty",
                chat_id=first_msg.chat_id,
                thread_id=first_msg.thread_id,
                media_group_id=first_msg.media_group_id,
                message_ids=[m.message_id for m in sorted_messages],
                failed_count=failed_count,
                image_disabled=image_disabled,
                file_disabled=file_disabled,
                image_too_large=image_too_large,
                file_too_large=file_too_large,
                image_download_failed=image_download_failed,
                file_download_failed=file_download_failed,
                image_save_failed=image_save_failed,
                file_save_failed=file_save_failed,
                unsupported_count=unsupported_count,
                max_image_bytes=max_image_bytes,
                max_file_bytes=max_file_bytes,
            )
            await self._send_message(
                first_msg.chat_id,
                _format_media_batch_failure(
                    image_disabled=image_disabled,
                    file_disabled=file_disabled,
                    image_too_large=image_too_large,
                    file_too_large=file_too_large,
                    image_download_failed=image_download_failed,
                    file_download_failed=file_download_failed,
                    image_download_detail=image_download_detail,
                    file_download_detail=file_download_detail,
                    image_save_failed=image_save_failed,
                    file_save_failed=file_save_failed,
                    unsupported=unsupported_count,
                    max_image_bytes=max_image_bytes,
                    max_file_bytes=max_file_bytes,
                ),
                thread_id=first_msg.thread_id,
                reply_to=first_msg.message_id,
            )
            return

        captions = [
            m.caption or "" for m in sorted_messages if m.caption and m.caption.strip()
        ]
        prompt_parts = []
        if captions:
            if len(captions) == 1:
                prompt_parts.append(captions[0].strip())
            else:
                prompt_parts.append("\n".join(f"- {c.strip()}" for c in captions))
        else:
            if saved_image_paths:
                prompt_parts.append(self._config.media.image_prompt)
            else:
                prompt_parts.append("Media received.")
        if saved_file_info:
            file_summary = ["\nFiles:"]
            for name, path, size in saved_file_info:
                file_summary.append(f"- {name} ({size} bytes) -> {path}")
            prompt_parts.append("\n".join(file_summary))
        if failed_count > 0:
            prompt_parts.append(f"\nFailed to process {failed_count} item(s).")

        inbox_dir = self._files_inbox_dir(record.workspace_path, topic_key)
        outbox_dir = self._files_outbox_pending_dir(record.workspace_path, topic_key)
        topic_dir = self._files_topic_dir(record.workspace_path, topic_key)
        hint = wrap_injected_context(
            FILES_HINT_TEMPLATE.format(
                inbox=str(inbox_dir),
                outbox=str(outbox_dir),
                topic_key=topic_key,
                topic_dir=str(topic_dir),
                max_bytes=self._config.media.max_file_bytes,
            )
        )
        prompt_parts.append(hint)
        combined_prompt = "\n\n".join(prompt_parts)

        input_items: Optional[list[dict[str, Any]]] = None
        if saved_image_paths:
            input_items = [{"type": "text", "text": combined_prompt}]
            for image_path in saved_image_paths:
                input_items.append({"type": "localImage", "path": str(image_path)})

        last_message = sorted_messages[-1]
        reply_to_id = last_message.message_id

        log_event(
            self._logger,
            logging.INFO,
            "telegram.media_batch.ready",
            chat_id=first_msg.chat_id,
            thread_id=first_msg.thread_id,
            image_count=len(saved_image_paths),
            file_count=len(saved_file_info),
            failed_count=failed_count,
            reply_to_message_id=reply_to_id,
        )
        await self._handle_normal_message(
            last_message,
            runtime,
            text_override=combined_prompt,
            input_items=input_items,
            record=record,
            placeholder_id=placeholder_id,
        )

    async def _download_telegram_file(
        self, file_id: str, *, max_bytes: Optional[int] = None
    ) -> tuple[bytes, Optional[str], Optional[int]]:
        payload = await self._bot.get_file(file_id)
        file_path = payload.get("file_path") if isinstance(payload, dict) else None
        file_size = payload.get("file_size") if isinstance(payload, dict) else None
        if file_size is not None and not isinstance(file_size, int):
            file_size = None
        if not isinstance(file_path, str) or not file_path:
            raise RuntimeError("Telegram getFile returned no file_path")
        if max_bytes is not None and max_bytes > 0:
            data = await self._bot.download_file(file_path, max_size_bytes=max_bytes)
        else:
            data = await self._bot.download_file(file_path)
        return data, file_path, file_size

    async def _send_voice_progress_message(
        self, record: PendingVoiceRecord, text: str
    ) -> Optional[int]:
        payload_text, parse_mode = self._prepare_outgoing_text(
            text,
            chat_id=record.chat_id,
            thread_id=record.thread_id,
            reply_to=record.message_id,
            workspace_path=record.workspace_path,
        )
        try:
            response = await self._bot.send_message(
                record.chat_id,
                payload_text,
                message_thread_id=record.thread_id,
                reply_to_message_id=record.message_id,
                parse_mode=parse_mode,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.voice.progress_failed",
                record_id=record.record_id,
                chat_id=record.chat_id,
                thread_id=record.thread_id,
                exc=exc,
            )
            return None
        message_id = response.get("message_id") if isinstance(response, dict) else None
        return message_id if isinstance(message_id, int) else None

    async def _update_voice_progress_message(
        self, record: PendingVoiceRecord, text: str
    ) -> None:
        if record.progress_message_id is None:
            return
        await self._edit_message_text(
            record.chat_id,
            record.progress_message_id,
            text,
        )

    async def _deliver_voice_transcript(
        self,
        record: PendingVoiceRecord,
        transcript_text: str,
    ) -> None:
        if record.transcript_message_id is None:
            transcript_message = self._format_voice_transcript_message(
                transcript_text,
                PLACEHOLDER_TEXT,
            )
            record.transcript_message_id = await self._send_voice_transcript_message(
                record.chat_id,
                transcript_message,
                thread_id=record.thread_id,
                reply_to=record.message_id,
            )
            await self._store.update_pending_voice(record)
        if record.transcript_message_id is None:
            raise RuntimeError("Failed to send voice transcript message")
        await self._update_voice_progress_message(record, "Voice note transcribed.")
        message = TelegramMessage(
            update_id=0,
            message_id=record.message_id,
            chat_id=record.chat_id,
            thread_id=record.thread_id,
            from_user_id=None,
            text=None,
            date=None,
            is_topic_message=record.thread_id is not None,
        )
        key = await self._resolve_topic_key(record.chat_id, record.thread_id)
        runtime = self._router.runtime_for(key)
        if self._config.concurrency.per_topic_queue:
            await runtime.queue.enqueue(
                lambda: self._handle_normal_message(
                    message,
                    runtime,
                    text_override=transcript_text,
                    send_placeholder=True,
                    transcript_message_id=record.transcript_message_id,
                    transcript_text=transcript_text,
                )
            )
        else:
            await self._handle_normal_message(
                message,
                runtime,
                text_override=transcript_text,
                send_placeholder=True,
                transcript_message_id=record.transcript_message_id,
                transcript_text=transcript_text,
            )

    def _image_storage_dir(self, workspace_path: str) -> Path:
        return (
            Path(workspace_path) / ".codex-autorunner" / "uploads" / "telegram-images"
        )

    def _choose_image_extension(
        self,
        *,
        file_path: Optional[str],
        file_name: Optional[str],
        mime_type: Optional[str],
    ) -> str:
        for candidate in (file_path, file_name):
            if candidate:
                suffix = Path(candidate).suffix.lower()
                if suffix in message_handlers.IMAGE_EXTS:
                    return suffix
        if mime_type:
            base = mime_type.lower().split(";", 1)[0].strip()
            mapped = message_handlers.IMAGE_CONTENT_TYPES.get(base)
            if mapped:
                return mapped
        return ".img"

    def _save_image_file(
        self,
        workspace_path: str,
        data: bytes,
        file_path: Optional[str],
        candidate: TelegramMediaCandidate,
    ) -> Path:
        images_dir = self._image_storage_dir(workspace_path)
        images_dir.mkdir(parents=True, exist_ok=True)
        ext = self._choose_image_extension(
            file_path=file_path,
            file_name=candidate.file_name,
            mime_type=candidate.mime_type,
        )
        token = secrets.token_hex(6)
        name = f"telegram-{int(time.time())}-{token}{ext}"
        path = images_dir / name
        path.write_bytes(data)
        return path

    def _files_root_dir(self, workspace_path: str) -> Path:
        return Path(workspace_path) / ".codex-autorunner" / "uploads" / "telegram-files"

    def _sanitize_topic_dir_name(self, key: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", key).strip("._-")
        if not cleaned:
            cleaned = "topic"
        if len(cleaned) > 80:
            digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:8]
            cleaned = f"{cleaned[:72]}-{digest}"
        return cleaned

    def _files_topic_dir(self, workspace_path: str, topic_key: str) -> Path:
        return self._files_root_dir(workspace_path) / self._sanitize_topic_dir_name(
            topic_key
        )

    def _files_inbox_dir(self, workspace_path: str, topic_key: str) -> Path:
        return self._files_topic_dir(workspace_path, topic_key) / "inbox"

    def _files_outbox_pending_dir(self, workspace_path: str, topic_key: str) -> Path:
        return self._files_topic_dir(workspace_path, topic_key) / "outbox" / "pending"

    def _files_outbox_sent_dir(self, workspace_path: str, topic_key: str) -> Path:
        return self._files_topic_dir(workspace_path, topic_key) / "outbox" / "sent"

    def _sanitize_filename_component(self, value: str, *, fallback: str) -> str:
        cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
        return cleaned or fallback

    def _choose_file_extension(
        self,
        *,
        file_name: Optional[str],
        file_path: Optional[str],
        mime_type: Optional[str],
    ) -> str:
        for candidate in (file_name, file_path):
            if candidate:
                suffix = Path(candidate).suffix
                if suffix:
                    return suffix
        if mime_type and mime_type.startswith("text/"):
            return ".txt"
        return ".bin"

    def _choose_file_stem(
        self, file_name: Optional[str], file_path: Optional[str]
    ) -> str:
        for candidate in (file_name, file_path):
            if candidate:
                stem = Path(candidate).stem
                if stem:
                    return stem
        return "file"

    def _save_inbox_file(
        self,
        workspace_path: str,
        topic_key: str,
        data: bytes,
        *,
        candidate: TelegramMediaCandidate,
        file_path: Optional[str],
    ) -> Path:
        inbox_dir = self._files_inbox_dir(workspace_path, topic_key)
        inbox_dir.mkdir(parents=True, exist_ok=True)
        stem = self._sanitize_filename_component(
            self._choose_file_stem(candidate.file_name, file_path),
            fallback="file",
        )
        ext = self._choose_file_extension(
            file_name=candidate.file_name,
            file_path=file_path,
            mime_type=candidate.mime_type,
        )
        token = secrets.token_hex(6)
        name = f"{stem}-{token}{ext}"
        path = inbox_dir / name
        path.write_bytes(data)
        return path

    def _format_file_prompt(
        self,
        caption_text: str,
        *,
        candidate: TelegramMediaCandidate,
        saved_path: Path,
        source_path: Optional[str],
        file_size: int,
        topic_key: str,
        workspace_path: str,
    ) -> str:
        header = caption_text.strip() or "File received."
        original_name = (
            candidate.file_name
            or (Path(source_path).name if source_path else None)
            or "unknown"
        )
        inbox_dir = self._files_inbox_dir(workspace_path, topic_key)
        outbox_dir = self._files_outbox_pending_dir(workspace_path, topic_key)
        topic_dir = self._files_topic_dir(workspace_path, topic_key)
        hint = wrap_injected_context(
            FILES_HINT_TEMPLATE.format(
                inbox=str(inbox_dir),
                outbox=str(outbox_dir),
                topic_key=topic_key,
                topic_dir=str(topic_dir),
                max_bytes=self._config.media.max_file_bytes,
            )
        )
        parts = [
            header,
            "",
            "File details:",
            f"- Name: {original_name}",
            f"- Size: {file_size} bytes",
        ]
        if candidate.mime_type:
            parts.append(f"- Mime: {candidate.mime_type}")
        parts.append(f"- Saved to: {saved_path}")
        parts.append("")
        parts.append(hint)
        return "\n".join(parts)

    def _format_bytes(self, size: int) -> str:
        if size < 1024:
            return f"{size} B"
        value = size / 1024
        for unit in ("KB", "MB", "GB", "TB"):
            if value < 1024:
                return f"{value:.1f} {unit}"
            value /= 1024
        return f"{value:.1f} PB"

    def _list_files(self, folder: Path) -> list[Path]:
        if not folder.exists():
            return []
        files: list[Path] = []
        for path in folder.iterdir():
            try:
                if path.is_file():
                    files.append(path)
            except OSError:
                continue

        def _mtime(entry: Path) -> float:
            try:
                return entry.stat().st_mtime
            except OSError:
                return 0.0

        return sorted(files, key=_mtime, reverse=True)

    async def _send_outbox_file(
        self,
        path: Path,
        *,
        sent_dir: Path,
        chat_id: int,
        thread_id: Optional[int],
        reply_to: Optional[int],
    ) -> bool:
        try:
            data = path.read_bytes()
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.files.outbox.read_failed",
                chat_id=chat_id,
                thread_id=thread_id,
                path=str(path),
                exc=exc,
            )
            return False
        try:
            await self._bot.send_document(
                chat_id,
                data,
                filename=path.name,
                message_thread_id=thread_id,
                reply_to_message_id=reply_to,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.files.outbox.send_failed",
                chat_id=chat_id,
                thread_id=thread_id,
                path=str(path),
                exc=exc,
            )
            return False
        try:
            sent_dir.mkdir(parents=True, exist_ok=True)
            destination = sent_dir / path.name
            if destination.exists():
                token = secrets.token_hex(3)
                destination = sent_dir / f"{path.stem}-{token}{path.suffix}"
            path.replace(destination)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.files.outbox.move_failed",
                chat_id=chat_id,
                thread_id=thread_id,
                path=str(path),
                exc=exc,
            )
            return False
        log_event(
            self._logger,
            logging.INFO,
            "telegram.files.outbox.sent",
            chat_id=chat_id,
            thread_id=thread_id,
            path=str(path),
        )
        return True

    async def _flush_outbox_files(
        self,
        record: Optional["TelegramTopicRecord"],
        *,
        chat_id: int,
        thread_id: Optional[int],
        reply_to: Optional[int],
        topic_key: Optional[str] = None,
    ) -> None:
        if (
            record is None
            or not record.workspace_path
            or not self._config.media.enabled
            or not self._config.media.files
        ):
            return
        if topic_key:
            key = topic_key
        else:
            key = await self._resolve_topic_key(chat_id, thread_id)
        pending_dir = self._files_outbox_pending_dir(record.workspace_path, key)
        if not pending_dir.exists():
            return
        files = self._list_files(pending_dir)
        if not files:
            return
        sent_dir = self._files_outbox_sent_dir(record.workspace_path, key)
        max_bytes = self._config.media.max_file_bytes
        for path in files:
            if not _path_within(pending_dir, path):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > max_bytes:
                await self._send_message(
                    chat_id,
                    f"Outbox file too large: {path.name} (max {max_bytes} bytes).",
                    thread_id=thread_id,
                    reply_to=reply_to,
                )
                continue
            await self._send_outbox_file(
                path,
                sent_dir=sent_dir,
                chat_id=chat_id,
                thread_id=thread_id,
                reply_to=reply_to,
            )

    async def _handle_interrupt(self, message: TelegramMessage, runtime: Any) -> None:
        await self._process_interrupt(
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            reply_to=message.message_id,
            runtime=runtime,
            message_id=message.message_id,
        )

    async def _handle_interrupt_callback(self, callback: TelegramCallbackQuery) -> None:
        if callback.chat_id is None or callback.message_id is None:
            await self._answer_callback(callback, "Cancel unavailable")
            return
        runtime = self._router.runtime_for(
            await self._resolve_topic_key(callback.chat_id, callback.thread_id)
        )
        await self._answer_callback(callback, "Stopping...")
        await self._process_interrupt(
            chat_id=callback.chat_id,
            thread_id=callback.thread_id,
            reply_to=callback.message_id,
            runtime=runtime,
            message_id=callback.message_id,
        )

    async def _process_interrupt(
        self,
        *,
        chat_id: int,
        thread_id: Optional[int],
        reply_to: Optional[int],
        runtime: Any,
        message_id: Optional[int],
    ) -> None:
        turn_id = runtime.current_turn_id
        key = await self._resolve_topic_key(chat_id, thread_id)
        if (
            turn_id
            and runtime.interrupt_requested
            and runtime.interrupt_turn_id == turn_id
        ):
            await self._send_message(
                chat_id,
                "Already stopping current turn.",
                thread_id=thread_id,
                reply_to=reply_to,
            )
            return
        pending_request_ids = [
            request_id
            for request_id, pending in self._pending_approvals.items()
            if (pending.topic_key == key)
            or (
                pending.topic_key is None
                and pending.chat_id == chat_id
                and pending.thread_id == thread_id
            )
        ]
        pending_question_ids = [
            request_id
            for request_id, pending in self._pending_questions.items()
            if (pending.topic_key == key)
            or (
                pending.topic_key is None
                and pending.chat_id == chat_id
                and pending.thread_id == thread_id
            )
        ]
        for request_id in pending_request_ids:
            pending = self._pending_approvals.pop(request_id, None)
            if pending and not pending.future.done():
                pending.future.set_result("cancel")
            await self._store.clear_pending_approval(request_id)
        for request_id in pending_question_ids:
            pending = self._pending_questions.pop(request_id, None)
            if pending and not pending.future.done():
                pending.future.set_result(None)
        if pending_request_ids:
            runtime.pending_request_id = None
        queued_turn_cancelled = False
        if (
            runtime.queued_turn_cancel is not None
            and not runtime.queued_turn_cancel.is_set()
        ):
            runtime.queued_turn_cancel.set()
            queued_turn_cancelled = True
        queued_cancelled = runtime.queue.cancel_pending()
        if not turn_id:
            active_cancelled = runtime.queue.cancel_active()
            pending_records = await self._store.pending_approvals_for_key(key)
            if pending_records:
                await self._store.clear_pending_approvals_for_key(key)
                runtime.pending_request_id = None
            pending_count = len(pending_records) if pending_records else 0
            pending_count += len(pending_request_ids)
            pending_question_count = len(pending_question_ids)
            if (
                queued_turn_cancelled
                or queued_cancelled
                or active_cancelled
                or pending_count
                or pending_question_count
            ):
                parts = []
                if queued_turn_cancelled:
                    parts.append("Cancelled queued turn.")
                if active_cancelled:
                    parts.append("Cancelled active job.")
                if queued_cancelled:
                    parts.append(f"Cancelled {queued_cancelled} queued job(s).")
                if pending_count:
                    parts.append(f"Cleared {pending_count} pending approval(s).")
                if pending_question_count:
                    parts.append(
                        f"Cleared {pending_question_count} pending question(s)."
                    )
                await self._send_message(
                    chat_id,
                    " ".join(parts),
                    thread_id=thread_id,
                    reply_to=reply_to,
                )
                return
            log_event(
                self._logger,
                logging.INFO,
                "telegram.interrupt.none",
                chat_id=chat_id,
                thread_id=thread_id,
                message_id=message_id,
            )
            await self._send_message(
                chat_id,
                "No active turn to interrupt.",
                thread_id=thread_id,
                reply_to=reply_to,
            )
            return
        runtime.interrupt_requested = True
        log_event(
            self._logger,
            logging.INFO,
            "telegram.interrupt.requested",
            chat_id=chat_id,
            thread_id=thread_id,
            message_id=message_id,
            turn_id=turn_id,
        )
        payload_text, parse_mode = self._prepare_outgoing_text(
            "Stopping current turn...",
            chat_id=chat_id,
            thread_id=thread_id,
            reply_to=reply_to,
        )
        response = await self._bot.send_message(
            chat_id,
            payload_text,
            message_thread_id=thread_id,
            reply_to_message_id=reply_to,
            parse_mode=parse_mode,
        )
        response_message_id = (
            response.get("message_id") if isinstance(response, dict) else None
        )
        codex_thread_id = None
        if runtime.current_turn_key and runtime.current_turn_key[1] == turn_id:
            codex_thread_id = runtime.current_turn_key[0]
        if isinstance(response_message_id, int):
            runtime.interrupt_message_id = response_message_id
            runtime.interrupt_turn_id = turn_id
            self._spawn_task(
                self._interrupt_timeout_check(
                    key,
                    turn_id,
                    response_message_id,
                )
            )
        self._spawn_task(
            self._dispatch_interrupt_request(
                turn_id=turn_id,
                codex_thread_id=codex_thread_id,
                runtime=runtime,
                chat_id=chat_id,
                thread_id=thread_id,
            )
        )

    async def _handle_bind(self, message: TelegramMessage, args: str) -> None:
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        if not args:
            options = self._list_manifest_repos()
            if not options:
                await self._send_message(
                    message.chat_id,
                    "Usage: /bind <repo_id> or /bind <path>.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            items = [(repo_id, repo_id) for repo_id in options]
            state = SelectionState(items=items)
            keyboard = self._build_bind_keyboard(state)
            self._bind_options[key] = state
            self._touch_cache_timestamp("bind_options", key)
            await self._send_message(
                message.chat_id,
                self._selection_prompt(BIND_PICKER_PROMPT, state),
                thread_id=message.thread_id,
                reply_to=message.message_id,
                reply_markup=keyboard,
            )
            return
        await self._bind_topic_with_arg(key, args, message)

    async def _bind_topic_by_repo_id(
        self,
        key: str,
        repo_id: str,
        callback: Optional[TelegramCallbackQuery] = None,
    ) -> None:
        self._bind_options.pop(key, None)
        resolved = self._resolve_workspace(repo_id)
        if resolved is None:
            await self._answer_callback(callback, "Repo not found")
            await self._finalize_selection(key, callback, "Repo not found.")
            return
        workspace_path, resolved_repo_id = resolved
        chat_id, thread_id = _split_topic_key(key)
        scope = self._topic_scope_id(resolved_repo_id, workspace_path)
        await self._router.set_topic_scope(chat_id, thread_id, scope)
        await self._router.bind_topic(
            chat_id,
            thread_id,
            workspace_path,
            repo_id=resolved_repo_id,
            scope=scope,
        )
        workspace_id = self._workspace_id_for_path(workspace_path)
        if workspace_id:
            await self._router.update_topic(
                chat_id,
                thread_id,
                lambda record: setattr(record, "workspace_id", workspace_id),
                scope=scope,
            )
        await self._answer_callback(callback, "Bound to repo")
        await self._finalize_selection(
            key,
            callback,
            f"Bound to {resolved_repo_id or workspace_path}.",
        )

    async def _bind_topic_with_arg(
        self, key: str, arg: str, message: TelegramMessage
    ) -> None:
        self._bind_options.pop(key, None)
        resolved = self._resolve_workspace(arg)
        if resolved is None:
            await self._send_message(
                message.chat_id,
                "Unknown repo or path. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        workspace_path, repo_id = resolved
        scope = self._topic_scope_id(repo_id, workspace_path)
        await self._router.set_topic_scope(message.chat_id, message.thread_id, scope)
        await self._router.bind_topic(
            message.chat_id,
            message.thread_id,
            workspace_path,
            repo_id=repo_id,
            scope=scope,
        )
        workspace_id = self._workspace_id_for_path(workspace_path)
        if workspace_id:
            await self._router.update_topic(
                message.chat_id,
                message.thread_id,
                lambda record: setattr(record, "workspace_id", workspace_id),
                scope=scope,
            )
        await self._send_message(
            message.chat_id,
            f"Bound to {repo_id or workspace_path}.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_new(self, message: TelegramMessage) -> None:
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        record = await self._router.get_topic(key)
        if record is None or not record.workspace_path:
            await self._send_message(
                message.chat_id,
                "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        agent = self._effective_agent(record)
        if agent == "opencode":
            supervisor = getattr(self, "_opencode_supervisor", None)
            if supervisor is None:
                await self._send_message(
                    message.chat_id,
                    "OpenCode backend unavailable; install opencode or switch to /agent codex.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            workspace_root = self._canonical_workspace_root(record.workspace_path)
            if workspace_root is None:
                await self._send_message(
                    message.chat_id,
                    "Workspace unavailable.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            try:
                client = await supervisor.get_client(workspace_root)
                session = await client.create_session(directory=str(workspace_root))
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.opencode.session.failed",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    exc=exc,
                )
                await self._send_message(
                    message.chat_id,
                    "Failed to start a new OpenCode thread.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            session_id = extract_session_id(session, allow_fallback_id=True)
            if not session_id:
                await self._send_message(
                    message.chat_id,
                    "Failed to start a new OpenCode thread.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return

            def apply(record: "TelegramTopicRecord") -> None:
                record.active_thread_id = session_id
                if session_id in record.thread_ids:
                    record.thread_ids.remove(session_id)
                record.thread_ids.insert(0, session_id)
                if len(record.thread_ids) > MAX_TOPIC_THREAD_HISTORY:
                    record.thread_ids = record.thread_ids[:MAX_TOPIC_THREAD_HISTORY]
                _set_thread_summary(
                    record,
                    session_id,
                    last_used_at=now_iso(),
                    workspace_path=record.workspace_path,
                    rollout_path=record.rollout_path,
                )

            await self._router.update_topic(message.chat_id, message.thread_id, apply)
            thread_id = session_id
        else:
            try:
                client = await self._client_for_workspace(record.workspace_path)
            except AppServerUnavailableError as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.app_server.unavailable",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    exc=exc,
                )
                await self._send_message(
                    message.chat_id,
                    "App server unavailable; try again or check logs.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            if client is None:
                await self._send_message(
                    message.chat_id,
                    "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            thread = await client.thread_start(record.workspace_path, agent=agent)
            if not await self._require_thread_workspace(
                message, record.workspace_path, thread, action="thread_start"
            ):
                return
            thread_id = _extract_thread_id(thread)
            if not thread_id:
                await self._send_message(
                    message.chat_id,
                    "Failed to start a new thread.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            await self._apply_thread_result(
                message.chat_id, message.thread_id, thread, active_thread_id=thread_id
            )
        effort_label = (
            record.effort or "default" if self._agent_supports_effort(agent) else "n/a"
        )
        await self._send_message(
            message.chat_id,
            "\n".join(
                [
                    f"Started new thread {thread_id}.",
                    f"Directory: {record.workspace_path or 'unbound'}",
                    f"Agent: {agent}",
                    f"Model: {record.model or 'default'}",
                    f"Effort: {effort_label}",
                ]
            ),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_opencode_resume(
        self,
        message: TelegramMessage,
        record: "TelegramTopicRecord",
        *,
        key: str,
        show_unscoped: bool,
        refresh: bool,
    ) -> None:
        if refresh:
            log_event(
                self._logger,
                logging.INFO,
                "telegram.opencode.resume.refresh_ignored",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
            )
        local_thread_ids: list[str] = []
        local_previews: dict[str, str] = {}
        local_thread_topics: dict[str, set[str]] = {}
        store_state = None
        if show_unscoped:
            store_state = await self._store.load()
            (
                local_thread_ids,
                local_previews,
                local_thread_topics,
            ) = _local_workspace_threads(
                store_state, record.workspace_path, current_key=key
            )
            for thread_id in record.thread_ids:
                local_thread_topics.setdefault(thread_id, set()).add(key)
                if thread_id not in local_thread_ids:
                    local_thread_ids.append(thread_id)
                cached_preview = _thread_summary_preview(record, thread_id)
                if cached_preview:
                    local_previews.setdefault(thread_id, cached_preview)
            allowed_thread_ids: set[str] = set()
            for thread_id in local_thread_ids:
                if thread_id in record.thread_ids:
                    allowed_thread_ids.add(thread_id)
                    continue
                for topic_key in local_thread_topics.get(thread_id, set()):
                    topic_record = (
                        store_state.topics.get(topic_key) if store_state else None
                    )
                    if topic_record and topic_record.agent == "opencode":
                        allowed_thread_ids.add(thread_id)
                        break
            if allowed_thread_ids:
                local_thread_ids = [
                    thread_id
                    for thread_id in local_thread_ids
                    if thread_id in allowed_thread_ids
                ]
                local_previews = {
                    thread_id: preview
                    for thread_id, preview in local_previews.items()
                    if thread_id in allowed_thread_ids
                }
            else:
                local_thread_ids = []
                local_previews = {}
        else:
            for thread_id in record.thread_ids:
                local_thread_ids.append(thread_id)
                cached_preview = _thread_summary_preview(record, thread_id)
                if cached_preview:
                    local_previews.setdefault(thread_id, cached_preview)
        if not local_thread_ids:
            await self._send_message(
                message.chat_id,
                _with_conversation_id(
                    "No previous OpenCode threads found for this topic. "
                    "Use /new to start one.",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        items: list[tuple[str, str]] = []
        seen_ids: set[str] = set()
        for thread_id in local_thread_ids:
            if thread_id in seen_ids:
                continue
            seen_ids.add(thread_id)
            preview = local_previews.get(thread_id)
            label = _format_missing_thread_label(thread_id, preview)
            items.append((thread_id, label))
        state = SelectionState(items=items)
        keyboard = self._build_resume_keyboard(state)
        self._resume_options[key] = state
        self._touch_cache_timestamp("resume_options", key)
        await self._send_message(
            message.chat_id,
            self._selection_prompt(RESUME_PICKER_PROMPT, state),
            thread_id=message.thread_id,
            reply_to=message.message_id,
            reply_markup=keyboard,
        )

    async def _handle_resume(self, message: TelegramMessage, args: str) -> None:
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        argv = self._parse_command_args(args)
        trimmed = args.strip()
        show_unscoped = False
        refresh = False
        remaining: list[str] = []
        for arg in argv:
            lowered = arg.lower()
            if lowered in ("--all", "all", "--unscoped", "unscoped"):
                show_unscoped = True
                continue
            if lowered in ("--refresh", "refresh"):
                refresh = True
                continue
            remaining.append(arg)
        if argv:
            trimmed = " ".join(remaining).strip()
        if trimmed.isdigit():
            state = self._resume_options.get(key)
            if state:
                page_items = _page_slice(state.items, state.page, DEFAULT_PAGE_SIZE)
                choice = int(trimmed)
                if 0 < choice <= len(page_items):
                    thread_id = page_items[choice - 1][0]
                    await self._resume_thread_by_id(key, thread_id)
                    return
        if trimmed and not trimmed.isdigit():
            if remaining and remaining[0].lower() in ("list", "ls"):
                trimmed = ""
            else:
                await self._resume_thread_by_id(key, trimmed)
                return
        record = await self._router.get_topic(key)
        if record is not None:
            agent = self._effective_agent(record)
            if not self._agent_supports_resume(agent):
                await self._send_message(
                    message.chat_id,
                    "Resume is only supported for the codex and opencode agents. Use /agent codex or /agent opencode to switch.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
        if record is None or not record.workspace_path:
            await self._send_message(
                message.chat_id,
                "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if self._effective_agent(record) == "opencode":
            await self._handle_opencode_resume(
                message,
                record,
                key=key,
                show_unscoped=show_unscoped,
                refresh=refresh,
            )
            return
        try:
            client = await self._client_for_workspace(record.workspace_path)
        except AppServerUnavailableError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.app_server.unavailable",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                "App server unavailable; try again or check logs.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if client is None:
            await self._send_message(
                message.chat_id,
                "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if not show_unscoped and not record.thread_ids:
            await self._send_message(
                message.chat_id,
                "No previous threads found for this topic. Use /new to start one.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        threads: list[dict[str, Any]] = []
        list_failed = False
        local_thread_ids: list[str] = []
        local_previews: dict[str, str] = {}
        local_thread_topics: dict[str, set[str]] = {}
        if show_unscoped:
            store_state = await self._store.load()
            local_thread_ids, local_previews, local_thread_topics = (
                _local_workspace_threads(
                    store_state, record.workspace_path, current_key=key
                )
            )
            for thread_id in record.thread_ids:
                local_thread_topics.setdefault(thread_id, set()).add(key)
                if thread_id not in local_thread_ids:
                    local_thread_ids.append(thread_id)
                cached_preview = _thread_summary_preview(record, thread_id)
                if cached_preview:
                    local_previews.setdefault(thread_id, cached_preview)
        limit = _resume_thread_list_limit(record.thread_ids)
        needed_ids = (
            None if show_unscoped or not record.thread_ids else set(record.thread_ids)
        )
        try:
            threads, _ = await self._list_threads_paginated(
                client,
                limit=limit,
                max_pages=THREAD_LIST_MAX_PAGES,
                needed_ids=needed_ids,
            )
        except Exception as exc:
            list_failed = True
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.resume.failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            if show_unscoped and not local_thread_ids:
                await self._send_message(
                    message.chat_id,
                    _with_conversation_id(
                        "Failed to list threads; check logs for details.",
                        chat_id=message.chat_id,
                        thread_id=message.thread_id,
                    ),
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
        entries_by_id: dict[str, dict[str, Any]] = {}
        for entry in threads:
            if not isinstance(entry, dict):
                continue
            entry_id = entry.get("id")
            if isinstance(entry_id, str):
                entries_by_id[entry_id] = entry
        candidates: list[dict[str, Any]] = []
        unscoped: list[dict[str, Any]] = []
        saw_path = False
        if show_unscoped:
            if threads:
                filtered, unscoped, saw_path = _partition_threads(
                    threads, record.workspace_path
                )
                seen_ids = {
                    entry.get("id")
                    for entry in filtered
                    if isinstance(entry.get("id"), str)
                }
                candidates = filtered + [
                    entry for entry in unscoped if entry.get("id") not in seen_ids
                ]
            if not candidates and not local_thread_ids:
                if unscoped and not saw_path:
                    await self._send_message(
                        message.chat_id,
                        _with_conversation_id(
                            "No workspace-tagged threads available. Use /resume --all to list "
                            "unscoped threads.",
                            chat_id=message.chat_id,
                            thread_id=message.thread_id,
                        ),
                        thread_id=message.thread_id,
                        reply_to=message.message_id,
                    )
                    return
                await self._send_message(
                    message.chat_id,
                    _with_conversation_id(
                        "No previous threads found for this workspace. "
                        "If threads exist, update the app-server to include cwd metadata or use /new.",
                        chat_id=message.chat_id,
                        thread_id=message.thread_id,
                    ),
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
        missing_ids: list[str] = []
        if show_unscoped:
            for thread_id in local_thread_ids:
                if thread_id not in entries_by_id:
                    missing_ids.append(thread_id)
        else:
            for thread_id in record.thread_ids:
                if thread_id not in entries_by_id:
                    missing_ids.append(thread_id)
        if refresh and missing_ids:
            refreshed = await self._refresh_thread_summaries(
                client,
                missing_ids,
                topic_keys_by_thread=local_thread_topics if show_unscoped else None,
                default_topic_key=key,
            )
            if refreshed:
                if show_unscoped:
                    store_state = await self._store.load()
                    local_thread_ids, local_previews, local_thread_topics = (
                        _local_workspace_threads(
                            store_state, record.workspace_path, current_key=key
                        )
                    )
                    for thread_id in record.thread_ids:
                        local_thread_topics.setdefault(thread_id, set()).add(key)
                        if thread_id not in local_thread_ids:
                            local_thread_ids.append(thread_id)
                        cached_preview = _thread_summary_preview(record, thread_id)
                        if cached_preview:
                            local_previews.setdefault(thread_id, cached_preview)
                else:
                    record = await self._router.get_topic(key) or record
        items: list[tuple[str, str]] = []
        button_labels: dict[str, str] = {}
        seen_item_ids: set[str] = set()
        if show_unscoped:
            for entry in candidates:
                candidate_id = entry.get("id")
                if not isinstance(candidate_id, str) or not candidate_id:
                    continue
                if candidate_id in seen_item_ids:
                    continue
                seen_item_ids.add(candidate_id)
                label = _format_thread_preview(entry)
                button_label = _extract_first_user_preview(entry)
                if button_label:
                    button_labels[candidate_id] = button_label
                if label == "(no preview)":
                    cached_preview = local_previews.get(candidate_id)
                    if cached_preview:
                        label = cached_preview
                items.append((candidate_id, label))
            for thread_id in local_thread_ids:
                if thread_id in seen_item_ids:
                    continue
                seen_item_ids.add(thread_id)
                cached_preview = local_previews.get(thread_id)
                label = (
                    cached_preview
                    if cached_preview
                    else _format_missing_thread_label(thread_id, None)
                )
                items.append((thread_id, label))
        else:
            if record.thread_ids:
                for thread_id in record.thread_ids:
                    entry_data = entries_by_id.get(thread_id)
                    if entry_data is None:
                        cached_preview = _thread_summary_preview(record, thread_id)
                        label = _format_missing_thread_label(thread_id, cached_preview)
                    else:
                        label = _format_thread_preview(entry_data)
                        button_label = _extract_first_user_preview(entry_data)
                        if button_label:
                            button_labels[thread_id] = button_label
                        if label == "(no preview)":
                            cached_preview = _thread_summary_preview(record, thread_id)
                            if cached_preview:
                                label = cached_preview
                    items.append((thread_id, label))
            else:
                for entry in entries_by_id.values():
                    entry_id = entry.get("id")
                    if not isinstance(entry_id, str) or not entry_id:
                        continue
                    label = _format_thread_preview(entry)
                    button_label = _extract_first_user_preview(entry)
                    if button_label:
                        button_labels[entry_id] = button_label
                    items.append((entry_id, label))
        if missing_ids:
            log_event(
                self._logger,
                logging.INFO,
                "telegram.resume.missing_thread_metadata",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                stored_count=len(record.thread_ids),
                listed_count=len(entries_by_id) if not show_unscoped else len(threads),
                missing_ids=missing_ids[:RESUME_MISSING_IDS_LOG_LIMIT],
                list_failed=list_failed,
            )
        if not items:
            await self._send_message(
                message.chat_id,
                _with_conversation_id(
                    "No resumable threads found.",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        state = SelectionState(items=items, button_labels=button_labels)
        keyboard = self._build_resume_keyboard(state)
        self._resume_options[key] = state
        self._touch_cache_timestamp("resume_options", key)
        await self._send_message(
            message.chat_id,
            self._selection_prompt(RESUME_PICKER_PROMPT, state),
            thread_id=message.thread_id,
            reply_to=message.message_id,
            reply_markup=keyboard,
        )

    async def _refresh_thread_summaries(
        self,
        client: CodexAppServerClient,
        thread_ids: Sequence[str],
        *,
        topic_keys_by_thread: Optional[dict[str, set[str]]] = None,
        default_topic_key: Optional[str] = None,
    ) -> set[str]:
        refreshed: set[str] = set()
        if not thread_ids:
            return refreshed
        unique_ids: list[str] = []
        seen: set[str] = set()
        for thread_id in thread_ids:
            if not isinstance(thread_id, str) or not thread_id:
                continue
            if thread_id in seen:
                continue
            seen.add(thread_id)
            unique_ids.append(thread_id)
            if len(unique_ids) >= RESUME_REFRESH_LIMIT:
                break
        for thread_id in unique_ids:
            try:
                result = await client.thread_resume(thread_id)
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.resume.refresh_failed",
                    thread_id=thread_id,
                    exc=exc,
                )
                continue
            user_preview, assistant_preview = _extract_thread_preview_parts(result)
            info = _extract_thread_info(result)
            workspace_path = info.get("workspace_path")
            rollout_path = info.get("rollout_path")
            if (
                user_preview is None
                and assistant_preview is None
                and workspace_path is None
                and rollout_path is None
            ):
                continue
            last_used_at = now_iso() if user_preview or assistant_preview else None

            def apply(
                record: TelegramTopicRecord,
                *,
                thread_id: str = thread_id,
                user_preview: Optional[str] = user_preview,
                assistant_preview: Optional[str] = assistant_preview,
                last_used_at: Optional[str] = last_used_at,
                workspace_path: Optional[str] = workspace_path,
                rollout_path: Optional[str] = rollout_path,
            ) -> None:
                _set_thread_summary(
                    record,
                    thread_id,
                    user_preview=user_preview,
                    assistant_preview=assistant_preview,
                    last_used_at=last_used_at,
                    workspace_path=workspace_path,
                    rollout_path=rollout_path,
                )

            keys = (
                topic_keys_by_thread.get(thread_id)
                if topic_keys_by_thread is not None
                else None
            )
            if keys:
                for key in keys:
                    await self._store.update_topic(key, apply)
            elif default_topic_key:
                await self._store.update_topic(default_topic_key, apply)
            else:
                continue
            refreshed.add(thread_id)
        return refreshed

    async def _list_threads_paginated(
        self,
        client: CodexAppServerClient,
        *,
        limit: int,
        max_pages: int,
        needed_ids: Optional[set[str]] = None,
    ) -> tuple[list[dict[str, Any]], set[str]]:
        entries: list[dict[str, Any]] = []
        found_ids: set[str] = set()
        seen_ids: set[str] = set()
        cursor: Optional[str] = None
        page_count = max(1, max_pages)
        for _ in range(page_count):
            payload = await client.thread_list(cursor=cursor, limit=limit)
            page_entries = _coerce_thread_list(payload)
            for entry in page_entries:
                if not isinstance(entry, dict):
                    continue
                thread_id = entry.get("id")
                if isinstance(thread_id, str):
                    if thread_id in seen_ids:
                        continue
                    seen_ids.add(thread_id)
                    found_ids.add(thread_id)
                entries.append(entry)
            if needed_ids is not None and needed_ids.issubset(found_ids):
                break
            cursor = _extract_thread_list_cursor(payload)
            if not cursor:
                break
        return entries, found_ids

    async def _resume_thread_by_id(
        self,
        key: str,
        thread_id: str,
        callback: Optional[TelegramCallbackQuery] = None,
    ) -> None:
        chat_id, thread_id_val = _split_topic_key(key)
        self._resume_options.pop(key, None)
        record = await self._router.get_topic(key)
        if record is not None and self._effective_agent(record) == "opencode":
            await self._resume_opencode_thread_by_id(key, thread_id, callback=callback)
            return
        if record is None or not record.workspace_path:
            await self._answer_callback(callback, "Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Topic not bound; use /bind before resuming.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        try:
            client = await self._client_for_workspace(record.workspace_path)
        except AppServerUnavailableError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.app_server.unavailable",
                chat_id=chat_id,
                thread_id=thread_id_val,
                exc=exc,
            )
            await self._answer_callback(callback, "Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "App server unavailable; try again or check logs.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        if client is None:
            await self._answer_callback(callback, "Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Topic not bound; use /bind before resuming.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        try:
            result = await client.thread_resume(thread_id)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.resume.failed",
                topic_key=key,
                thread_id=thread_id,
                exc=exc,
            )
            await self._answer_callback(callback, "Resume failed")
            chat_id, thread_id_val = _split_topic_key(key)
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Failed to resume thread; check logs for details.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        info = _extract_thread_info(result)
        resumed_path = info.get("workspace_path")
        if record is None or not record.workspace_path:
            await self._answer_callback(callback, "Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Topic not bound; use /bind before resuming.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        if not isinstance(resumed_path, str):
            await self._answer_callback(callback, "Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Thread metadata missing workspace path; resume aborted to avoid cross-worktree mixups.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        try:
            workspace_root = Path(record.workspace_path).expanduser().resolve()
            resumed_root = Path(resumed_path).expanduser().resolve()
        except Exception:
            await self._answer_callback(callback, "Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Thread workspace path is invalid; resume aborted.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        if not _paths_compatible(workspace_root, resumed_root):
            await self._answer_callback(callback, "Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Thread belongs to a different workspace; resume aborted.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        conflict_key = await self._find_thread_conflict(thread_id, key=key)
        if conflict_key:
            await self._answer_callback(callback, "Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Thread is already active in another topic; resume aborted.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.resume.conflict",
                topic_key=key,
                thread_id=thread_id,
                conflict_topic=conflict_key,
            )
            return
        updated_record = await self._apply_thread_result(
            chat_id,
            thread_id_val,
            result,
            active_thread_id=thread_id,
            overwrite_defaults=True,
        )
        await self._answer_callback(callback, "Resumed thread")
        message = _format_resume_summary(
            thread_id,
            result,
            workspace_path=updated_record.workspace_path,
            model=updated_record.model,
            effort=updated_record.effort,
        )
        await self._finalize_selection(key, callback, message)

    async def _resume_opencode_thread_by_id(
        self,
        key: str,
        thread_id: str,
        callback: Optional[TelegramCallbackQuery] = None,
    ) -> None:
        chat_id, thread_id_val = _split_topic_key(key)
        self._resume_options.pop(key, None)
        record = await self._router.get_topic(key)
        if record is None or not record.workspace_path:
            await self._answer_callback(callback, "Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Topic not bound; use /bind before resuming.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        supervisor = getattr(self, "_opencode_supervisor", None)
        if supervisor is None:
            await self._answer_callback(callback, "Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "OpenCode backend unavailable; install opencode or switch to /agent codex.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        workspace_root = self._canonical_workspace_root(record.workspace_path)
        if workspace_root is None:
            await self._answer_callback(callback, "Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Workspace unavailable; resume aborted.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        try:
            client = await supervisor.get_client(workspace_root)
            session = await client.get_session(thread_id)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.opencode.resume.failed",
                topic_key=key,
                thread_id=thread_id,
                exc=exc,
            )
            await self._answer_callback(callback, "Resume failed")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Failed to resume OpenCode thread; check logs for details.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            return
        resumed_path = _extract_opencode_session_path(session)
        if resumed_path:
            try:
                workspace_root = Path(record.workspace_path).expanduser().resolve()
                resumed_root = Path(resumed_path).expanduser().resolve()
            except Exception:
                await self._answer_callback(callback, "Resume aborted")
                await self._finalize_selection(
                    key,
                    callback,
                    _with_conversation_id(
                        "Thread workspace path is invalid; resume aborted.",
                        chat_id=chat_id,
                        thread_id=thread_id_val,
                    ),
                )
                return
            if not _paths_compatible(workspace_root, resumed_root):
                await self._answer_callback(callback, "Resume aborted")
                await self._finalize_selection(
                    key,
                    callback,
                    _with_conversation_id(
                        "Thread belongs to a different workspace; resume aborted.",
                        chat_id=chat_id,
                        thread_id=thread_id_val,
                    ),
                )
                return
        conflict_key = await self._find_thread_conflict(thread_id, key=key)
        if conflict_key:
            await self._answer_callback(callback, "Resume aborted")
            await self._finalize_selection(
                key,
                callback,
                _with_conversation_id(
                    "Thread is already active in another topic; resume aborted.",
                    chat_id=chat_id,
                    thread_id=thread_id_val,
                ),
            )
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.resume.conflict",
                topic_key=key,
                thread_id=thread_id,
                conflict_topic=conflict_key,
            )
            return

        def apply(record: "TelegramTopicRecord") -> None:
            record.active_thread_id = thread_id
            if thread_id in record.thread_ids:
                record.thread_ids.remove(thread_id)
            record.thread_ids.insert(0, thread_id)
            if len(record.thread_ids) > MAX_TOPIC_THREAD_HISTORY:
                record.thread_ids = record.thread_ids[:MAX_TOPIC_THREAD_HISTORY]
            _set_thread_summary(
                record,
                thread_id,
                last_used_at=now_iso(),
                workspace_path=record.workspace_path,
                rollout_path=record.rollout_path,
            )

        updated_record = await self._router.update_topic(chat_id, thread_id_val, apply)
        await self._answer_callback(callback, "Resumed thread")
        summary = None
        if updated_record is not None:
            summary = updated_record.thread_summaries.get(thread_id)
        entry: dict[str, Any] = {}
        if summary is not None:
            entry = {
                "user_preview": summary.user_preview,
                "assistant_preview": summary.assistant_preview,
            }
        message = _format_resume_summary(
            thread_id,
            entry,
            workspace_path=updated_record.workspace_path if updated_record else None,
            model=updated_record.model if updated_record else None,
            effort=updated_record.effort if updated_record else None,
        )
        await self._finalize_selection(key, callback, message)

    async def _handle_status(
        self, message: TelegramMessage, _args: str = "", runtime: Optional[Any] = None
    ) -> None:
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        record = await self._router.ensure_topic(message.chat_id, message.thread_id)
        await self._refresh_workspace_id(key, record)
        if runtime is None:
            runtime = self._router.runtime_for(key)
        approval_policy, sandbox_policy = self._effective_policies(record)
        agent = self._effective_agent(record)
        effort_label = (
            record.effort or "default" if self._agent_supports_effort(agent) else "n/a"
        )
        lines = [
            f"Workspace: {record.workspace_path or 'unbound'}",
            f"Workspace ID: {record.workspace_id or 'unknown'}",
            f"Active thread: {record.active_thread_id or 'none'}",
            f"Active turn: {runtime.current_turn_id or 'none'}",
            f"Agent: {agent}",
            f"Resume: {'supported' if self._agent_supports_resume(agent) else 'unsupported'}",
            f"Model: {record.model or 'default'}",
            f"Effort: {effort_label}",
            f"Approval mode: {record.approval_mode}",
            f"Approval policy: {approval_policy or 'default'}",
            f"Sandbox policy: {_format_sandbox_policy(sandbox_policy)}",
        ]
        pending = await self._store.pending_approvals_for_key(key)
        if pending:
            lines.append(f"Pending approvals: {len(pending)}")
            if len(pending) == 1:
                age = _approval_age_seconds(pending[0].created_at)
                age_label = f"{age}s" if isinstance(age, int) else "unknown age"
                lines.append(f"Pending request: {pending[0].request_id} ({age_label})")
            else:
                preview = ", ".join(item.request_id for item in pending[:3])
                suffix = "" if len(pending) <= 3 else "..."
                lines.append(f"Pending requests: {preview}{suffix}")
        if record.summary:
            lines.append(f"Summary: {record.summary}")
        if record.active_thread_id:
            token_usage = self._token_usage_by_thread.get(record.active_thread_id)
            lines.extend(_format_token_usage(token_usage))
        rate_limits = await self._read_rate_limits(record.workspace_path, agent=agent)
        lines.extend(_format_rate_limits(rate_limits))
        if not record.workspace_path:
            lines.append("Use /bind <repo_id> or /bind <path>.")
        await self._send_message(
            message.chat_id,
            "\n".join(lines),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    def _format_file_listing(self, title: str, files: list[Path]) -> str:
        if not files:
            return f"{title}: (empty)"
        lines = [f"{title} ({len(files)}):"]
        for path in files[:50]:
            try:
                stats = path.stat()
            except OSError:
                continue
            mtime = datetime.fromtimestamp(stats.st_mtime).isoformat(timespec="seconds")
            lines.append(
                f"- {path.name} ({self._format_bytes(stats.st_size)}, {mtime})"
            )
        if len(files) > 50:
            lines.append(f"... and {len(files) - 50} more")
        return "\n".join(lines)

    def _delete_files_in_dir(self, folder: Path) -> int:
        if not folder.exists():
            return 0
        deleted = 0
        for path in folder.iterdir():
            try:
                if path.is_file():
                    path.unlink()
                    deleted += 1
            except OSError:
                continue
        return deleted

    async def _handle_files(
        self, message: TelegramMessage, args: str, _runtime: Any
    ) -> None:
        if not self._config.media.enabled or not self._config.media.files:
            await self._send_message(
                message.chat_id,
                "File handling is disabled.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        record = await self._require_bound_record(message)
        if not record:
            return
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        inbox_dir = self._files_inbox_dir(record.workspace_path, key)
        pending_dir = self._files_outbox_pending_dir(record.workspace_path, key)
        sent_dir = self._files_outbox_sent_dir(record.workspace_path, key)
        argv = self._parse_command_args(args)
        if not argv:
            inbox_items = self._list_files(inbox_dir)
            pending_items = self._list_files(pending_dir)
            sent_items = self._list_files(sent_dir)
            text = "\n".join(
                [
                    f"Inbox: {len(inbox_items)} item(s)",
                    f"Outbox pending: {len(pending_items)} item(s)",
                    f"Outbox sent: {len(sent_items)} item(s)",
                    "Usage: /files inbox|outbox|clear inbox|outbox|all|send <filename>",
                ]
            )
            await self._send_message(
                message.chat_id,
                text,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        subcommand = argv[0].lower()
        if subcommand == "inbox":
            files = self._list_files(inbox_dir)
            text = self._format_file_listing("Inbox", files)
            await self._send_message(
                message.chat_id,
                text,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if subcommand == "outbox":
            pending_items = self._list_files(pending_dir)
            sent_items = self._list_files(sent_dir)
            text = "\n".join(
                [
                    self._format_file_listing("Outbox pending", pending_items),
                    "",
                    self._format_file_listing("Outbox sent", sent_items),
                ]
            )
            await self._send_message(
                message.chat_id,
                text,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        try:
            client = await self._client_for_workspace(record.workspace_path)
        except AppServerUnavailableError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.app_server.unavailable",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                "App server unavailable; try again or check logs.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if client is None:
            await self._send_message(
                message.chat_id,
                "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
            target = argv[1].lower()
            deleted = 0
            if target == "inbox":
                deleted = self._delete_files_in_dir(inbox_dir)
            elif target == "outbox":
                deleted = self._delete_files_in_dir(pending_dir)
                deleted += self._delete_files_in_dir(sent_dir)
            elif target == "all":
                deleted = self._delete_files_in_dir(inbox_dir)
                deleted += self._delete_files_in_dir(pending_dir)
                deleted += self._delete_files_in_dir(sent_dir)
            else:
                await self._send_message(
                    message.chat_id,
                    "Usage: /files clear inbox|outbox|all",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            await self._send_message(
                message.chat_id,
                f"Deleted {deleted} file(s).",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        try:
            client = await self._client_for_workspace(record.workspace_path)
        except AppServerUnavailableError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.app_server.unavailable",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                "App server unavailable; try again or check logs.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if client is None:
            await self._send_message(
                message.chat_id,
                "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
            name = Path(argv[1]).name
            candidate = pending_dir / name
            if not _path_within(pending_dir, candidate) or not candidate.is_file():
                await self._send_message(
                    message.chat_id,
                    f"Outbox pending file not found: {name}",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            size = candidate.stat().st_size
            max_bytes = self._config.media.max_file_bytes
            if size > max_bytes:
                await self._send_message(
                    message.chat_id,
                    f"Outbox file too large: {name} (max {max_bytes} bytes).",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            success = await self._send_outbox_file(
                candidate,
                sent_dir=sent_dir,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            result = "Sent." if success else "Failed to send."
            await self._send_message(
                message.chat_id,
                result,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        await self._send_message(
            message.chat_id,
            "Usage: /files inbox|outbox|clear inbox|outbox|all|send <filename>",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_debug(
        self, message: TelegramMessage, _args: str = "", _runtime: Optional[Any] = None
    ) -> None:
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        record = await self._router.get_topic(key)
        scope = None
        try:
            chat_id, thread_id, scope = parse_topic_key(key)
            base_key = topic_key(chat_id, thread_id)
        except ValueError:
            base_key = key
        lines = [
            f"Topic key: {key}",
            f"Base key: {base_key}",
            f"Scope: {scope or 'none'}",
        ]
        if record is None:
            lines.append("Record: missing")
            await self._send_message(
                message.chat_id,
                "\n".join(lines),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        await self._refresh_workspace_id(key, record)
        workspace_path = record.workspace_path or "unbound"
        canonical_path = "unbound"
        if record.workspace_path:
            try:
                canonical_path = str(Path(record.workspace_path).expanduser().resolve())
            except Exception:
                canonical_path = "invalid"
        lines.extend(
            [
                f"Workspace: {workspace_path}",
                f"Workspace ID: {record.workspace_id or 'unknown'}",
                f"Workspace (canonical): {canonical_path}",
                f"Active thread: {record.active_thread_id or 'none'}",
                f"Thread IDs: {len(record.thread_ids)}",
                f"Cached summaries: {len(record.thread_summaries)}",
            ]
        )
        preview_ids = record.thread_ids[:3]
        if preview_ids:
            lines.append("Preview samples:")
            for preview_thread_id in preview_ids:
                preview = _thread_summary_preview(record, preview_thread_id)
                label = preview or "(no cached preview)"
                lines.append(f"{preview_thread_id}: {_compact_preview(label, 120)}")
        await self._send_message(
            message.chat_id,
            "\n".join(lines),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_ids(
        self, message: TelegramMessage, _args: str = "", _runtime: Optional[Any] = None
    ) -> None:
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        lines = [
            f"Chat ID: {message.chat_id}",
            f"Thread ID: {message.thread_id or 'none'}",
            f"User ID: {message.from_user_id or 'unknown'}",
            f"Topic key: {key}",
            "Allowlist example:",
            f"telegram_bot.allowed_chat_ids: [{message.chat_id}]",
        ]
        if message.from_user_id is not None:
            lines.append(f"telegram_bot.allowed_user_ids: [{message.from_user_id}]")
        await self._send_message(
            message.chat_id,
            "\n".join(lines),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _read_rate_limits(
        self, workspace_path: Optional[str], *, agent: str
    ) -> Optional[dict[str, Any]]:
        if self._agent_rate_limit_source(agent) != "app_server":
            return None
        try:
            client = await self._client_for_workspace(workspace_path)
        except AppServerUnavailableError:
            return None
        if client is None:
            return None
        for method in ("account/rateLimits/read", "account/read"):
            try:
                result = await client.request(method, params=None, timeout=5.0)
            except (CodexAppServerError, asyncio.TimeoutError):
                continue
            rate_limits = _extract_rate_limits(result)
            if rate_limits:
                return rate_limits
        return None

    async def _handle_approvals(
        self, message: TelegramMessage, args: str, _runtime: Optional[Any] = None
    ) -> None:
        argv = self._parse_command_args(args)
        record = await self._router.ensure_topic(message.chat_id, message.thread_id)
        if not argv:
            approval_policy, sandbox_policy = self._effective_policies(record)
            await self._send_message(
                message.chat_id,
                "\n".join(
                    [
                        f"Approval mode: {record.approval_mode}",
                        f"Approval policy: {approval_policy or 'default'}",
                        f"Sandbox policy: {_format_sandbox_policy(sandbox_policy)}",
                        "Usage: /approvals yolo|safe|read-only|auto|full-access",
                    ]
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        persist = False
        if "--persist" in argv:
            persist = True
            argv = [arg for arg in argv if arg != "--persist"]
        if not argv:
            await self._send_message(
                message.chat_id,
                "Usage: /approvals yolo|safe|read-only|auto|full-access",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        mode = argv[0].lower()
        if mode in ("yolo", "off", "disable", "disabled"):
            await self._router.set_approval_mode(
                message.chat_id, message.thread_id, "yolo"
            )
            await self._router.update_topic(
                message.chat_id,
                message.thread_id,
                lambda record: _clear_policy_overrides(record),
            )
            await self._send_message(
                message.chat_id,
                _format_persist_note("Approval mode set to yolo.", persist=persist),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if mode in ("safe", "on", "enable", "enabled"):
            await self._router.set_approval_mode(
                message.chat_id, message.thread_id, "safe"
            )
            await self._router.update_topic(
                message.chat_id,
                message.thread_id,
                lambda record: _clear_policy_overrides(record),
            )
            await self._send_message(
                message.chat_id,
                _format_persist_note("Approval mode set to safe.", persist=persist),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        preset = _normalize_approval_preset(mode)
        if mode == "preset" and len(argv) > 1:
            preset = _normalize_approval_preset(argv[1])
        if preset:
            approval_policy, sandbox_policy = APPROVAL_PRESETS[preset]
            await self._router.update_topic(
                message.chat_id,
                message.thread_id,
                lambda record: _set_policy_overrides(
                    record,
                    approval_policy=approval_policy,
                    sandbox_policy=sandbox_policy,
                ),
            )
            await self._send_message(
                message.chat_id,
                _format_persist_note(
                    f"Approval policy set to {approval_policy} with sandbox {sandbox_policy}.",
                    persist=persist,
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        approval_policy = argv[0] if argv[0] in APPROVAL_POLICY_VALUES else None
        if approval_policy:
            sandbox_policy = argv[1] if len(argv) > 1 else None
            await self._router.update_topic(
                message.chat_id,
                message.thread_id,
                lambda record: _set_policy_overrides(
                    record,
                    approval_policy=approval_policy,
                    sandbox_policy=sandbox_policy,
                ),
            )
            await self._send_message(
                message.chat_id,
                _format_persist_note(
                    f"Approval policy set to {approval_policy} with sandbox {sandbox_policy or 'default'}.",
                    persist=persist,
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        await self._send_message(
            message.chat_id,
            "Usage: /approvals yolo|safe|read-only|auto|full-access",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_model(
        self, message: TelegramMessage, args: str, _runtime: Any
    ) -> None:
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        self._model_options.pop(key, None)
        self._model_pending.pop(key, None)
        record = await self._router.get_topic(key)
        agent = self._effective_agent(record)
        supports_effort = self._agent_supports_effort(agent)
        list_params = {
            "cursor": None,
            "limit": DEFAULT_MODEL_LIST_LIMIT,
            "agent": agent,
        }
        try:
            client = await self._client_for_workspace(
                record.workspace_path if record else None
            )
        except AppServerUnavailableError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.app_server.unavailable",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                "App server unavailable; try again or check logs.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if client is None:
            await self._send_message(
                message.chat_id,
                "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        argv = self._parse_command_args(args)
        if not argv:
            try:
                result = await self._fetch_model_list(
                    record,
                    agent=agent,
                    client=client,
                    list_params=list_params,
                )
            except OpenCodeSupervisorError as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.model.list.failed",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    agent=agent,
                    exc=exc,
                )
                await self._send_message(
                    message.chat_id,
                    "OpenCode backend unavailable; install opencode or switch to /agent codex.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.model.list.failed",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    agent=agent,
                    exc=exc,
                )
                await self._send_message(
                    message.chat_id,
                    _with_conversation_id(
                        "Failed to list models; check logs for details.",
                        chat_id=message.chat_id,
                        thread_id=message.thread_id,
                    ),
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            options = _coerce_model_options(result, include_efforts=supports_effort)
            if not options:
                await self._send_message(
                    message.chat_id,
                    "No models found.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            items = [(option.model_id, option.label) for option in options]
            state = ModelPickerState(
                items=items,
                options={option.model_id: option for option in options},
            )
            self._model_options[key] = state
            self._touch_cache_timestamp("model_options", key)
            try:
                keyboard = self._build_model_keyboard(state)
            except ValueError:
                self._model_options.pop(key, None)
                await self._send_message(
                    message.chat_id,
                    _format_model_list(
                        result,
                        include_efforts=supports_effort,
                        set_hint=(
                            "Use /model <provider/model> to set."
                            if not supports_effort
                            else None
                        ),
                    ),
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            await self._send_message(
                message.chat_id,
                self._selection_prompt(MODEL_PICKER_PROMPT, state),
                thread_id=message.thread_id,
                reply_to=message.message_id,
                reply_markup=keyboard,
            )
            return
        if argv[0].lower() in ("list", "ls"):
            try:
                result = await self._fetch_model_list(
                    record,
                    agent=agent,
                    client=client,
                    list_params=list_params,
                )
            except OpenCodeSupervisorError as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.model.list.failed",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    agent=agent,
                    exc=exc,
                )
                await self._send_message(
                    message.chat_id,
                    "OpenCode backend unavailable; install opencode or switch to /agent codex.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.model.list.failed",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    agent=agent,
                    exc=exc,
                )
                await self._send_message(
                    message.chat_id,
                    _with_conversation_id(
                        "Failed to list models; check logs for details.",
                        chat_id=message.chat_id,
                        thread_id=message.thread_id,
                    ),
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            await self._send_message(
                message.chat_id,
                _format_model_list(
                    result,
                    include_efforts=supports_effort,
                    set_hint=(
                        "Use /model <provider/model> to set."
                        if not supports_effort
                        else None
                    ),
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if argv[0].lower() in ("clear", "reset"):
            await self._router.update_topic(
                message.chat_id,
                message.thread_id,
                lambda record: _set_model_overrides(record, None, clear_effort=True),
            )
            await self._send_message(
                message.chat_id,
                "Model overrides cleared.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if argv[0].lower() == "set" and len(argv) > 1:
            model = argv[1]
            effort = argv[2] if len(argv) > 2 else None
        else:
            model = argv[0]
            effort = argv[1] if len(argv) > 1 else None
        if effort and not supports_effort:
            await self._send_message(
                message.chat_id,
                "Reasoning effort is only supported for the codex agent.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if not supports_effort and "/" not in model:
            await self._send_message(
                message.chat_id,
                "OpenCode models must be in provider/model format (e.g., openai/gpt-4o).",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if effort and effort not in VALID_REASONING_EFFORTS:
            await self._send_message(
                message.chat_id,
                f"Unknown effort '{effort}'. Allowed: {', '.join(sorted(VALID_REASONING_EFFORTS))}.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        await self._router.update_topic(
            message.chat_id,
            message.thread_id,
            lambda record: _set_model_overrides(
                record,
                model,
                effort=effort,
                clear_effort=not supports_effort,
            ),
        )
        effort_note = f" (effort={effort})" if effort and supports_effort else ""
        await self._send_message(
            message.chat_id,
            f"Model set to {model}{effort_note}. Will apply on the next turn.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _start_codex_review(
        self,
        message: TelegramMessage,
        runtime: Any,
        *,
        record: TelegramTopicRecord,
        thread_id: str,
        target: dict[str, Any],
        delivery: str,
    ) -> None:
        try:
            client = await self._client_for_workspace(record.workspace_path)
        except AppServerUnavailableError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.app_server.unavailable",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                "App server unavailable; try again or check logs.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if client is None:
            await self._send_message(
                message.chat_id,
                "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        agent = self._effective_agent(record)
        log_event(
            self._logger,
            logging.INFO,
            "telegram.review.starting",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            codex_thread_id=thread_id,
            delivery=delivery,
            target=target.get("type"),
            agent=agent,
        )
        approval_policy, sandbox_policy = self._effective_policies(record)
        supports_effort = self._agent_supports_effort(agent)
        review_kwargs: dict[str, Any] = {}
        if approval_policy:
            review_kwargs["approval_policy"] = approval_policy
        if sandbox_policy:
            review_kwargs["sandbox_policy"] = sandbox_policy
        if agent:
            review_kwargs["agent"] = agent
        if record.model:
            review_kwargs["model"] = record.model
        if record.effort and supports_effort:
            review_kwargs["effort"] = record.effort
        if record.summary:
            review_kwargs["summary"] = record.summary
        if record.workspace_path:
            review_kwargs["cwd"] = record.workspace_path
        turn_handle = None
        turn_key: Optional[TurnKey] = None
        placeholder_id: Optional[int] = None
        turn_started_at: Optional[float] = None
        turn_elapsed_seconds: Optional[float] = None
        queued = False
        placeholder_text = PLACEHOLDER_TEXT
        try:
            turn_semaphore = self._ensure_turn_semaphore()
            queued = turn_semaphore.locked()
            if queued:
                placeholder_text = QUEUED_PLACEHOLDER_TEXT
            placeholder_id = await self._send_placeholder(
                message.chat_id,
                thread_id=message.thread_id,
                reply_to=message.message_id,
                text=placeholder_text,
                reply_markup=self._interrupt_keyboard(),
            )
            queue_started_at = time.monotonic()
            acquired = await self._await_turn_slot(
                turn_semaphore,
                runtime,
                message=message,
                placeholder_id=placeholder_id,
                queued=queued,
            )
            if not acquired:
                runtime.interrupt_requested = False
                return
            try:
                queue_wait_ms = int((time.monotonic() - queue_started_at) * 1000)
                log_event(
                    self._logger,
                    logging.INFO,
                    "telegram.review.queue_wait",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    codex_thread_id=thread_id,
                    queue_wait_ms=queue_wait_ms,
                    queued=queued,
                    max_parallel_turns=self._config.concurrency.max_parallel_turns,
                    per_topic_queue=self._config.concurrency.per_topic_queue,
                )
                if (
                    queued
                    and placeholder_id is not None
                    and placeholder_text != PLACEHOLDER_TEXT
                ):
                    await self._edit_message_text(
                        message.chat_id,
                        placeholder_id,
                        PLACEHOLDER_TEXT,
                    )
                turn_handle = await client.review_start(
                    thread_id,
                    target=target,
                    delivery=delivery,
                    **review_kwargs,
                )
                turn_started_at = time.monotonic()
                turn_key = self._turn_key(thread_id, turn_handle.turn_id)
                runtime.current_turn_id = turn_handle.turn_id
                runtime.current_turn_key = turn_key
                topic_key = await self._resolve_topic_key(
                    message.chat_id, message.thread_id
                )
                ctx = TurnContext(
                    topic_key=topic_key,
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    codex_thread_id=thread_id,
                    reply_to_message_id=message.message_id,
                    placeholder_message_id=placeholder_id,
                )
                if turn_key is None or not self._register_turn_context(
                    turn_key, turn_handle.turn_id, ctx
                ):
                    runtime.current_turn_id = None
                    runtime.current_turn_key = None
                    runtime.interrupt_requested = False
                    await self._send_message(
                        message.chat_id,
                        "Turn collision detected; please retry.",
                        thread_id=message.thread_id,
                        reply_to=message.message_id,
                    )
                    if placeholder_id is not None:
                        await self._delete_message(message.chat_id, placeholder_id)
                    return
                await self._start_turn_progress(
                    turn_key,
                    ctx=ctx,
                    agent=self._effective_agent(record),
                    model=record.model,
                    label="working",
                )
                result = await self._wait_for_turn_result(
                    client,
                    turn_handle,
                    timeout_seconds=self._config.app_server_turn_timeout_seconds,
                    topic_key=topic_key,
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                )
                if turn_started_at is not None:
                    turn_elapsed_seconds = time.monotonic() - turn_started_at
            finally:
                turn_semaphore.release()
        except Exception as exc:
            if turn_handle is not None:
                if turn_key is not None:
                    self._turn_contexts.pop(turn_key, None)
            runtime.current_turn_id = None
            runtime.current_turn_key = None
            runtime.interrupt_requested = False
            failure_message = "Codex review failed; check logs for details."
            reason = "review_failed"
            if isinstance(exc, asyncio.TimeoutError):
                failure_message = (
                    "Codex review timed out; interrupting now. "
                    "Please resend the review command in a moment."
                )
                reason = "turn_timeout"
            elif isinstance(exc, CodexAppServerDisconnected):
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.app_server.disconnected_during_review",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    turn_id=turn_handle.turn_id if turn_handle else None,
                )
                failure_message = (
                    "Codex app-server disconnected; recovering now. "
                    "Your review did not complete. Please resend the review command in a moment."
                )
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.review.failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
                reason=reason,
            )
            response_sent = await self._deliver_turn_response(
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                reply_to=message.message_id,
                placeholder_id=placeholder_id,
                response=_with_conversation_id(
                    failure_message,
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                ),
            )
            if response_sent:
                await self._delete_message(message.chat_id, placeholder_id)
            return
        finally:
            if turn_handle is not None:
                if turn_key is not None:
                    self._turn_contexts.pop(turn_key, None)
                    self._clear_thinking_preview(turn_key)
                    self._clear_turn_progress(turn_key)
            runtime.current_turn_id = None
            runtime.current_turn_key = None
            runtime.interrupt_requested = False
        response = _compose_agent_response(
            result.agent_messages, errors=result.errors, status=result.status
        )
        if thread_id and result.agent_messages:
            assistant_preview = _preview_from_text(
                response, RESUME_PREVIEW_ASSISTANT_LIMIT
            )
            if assistant_preview:
                await self._router.update_topic(
                    message.chat_id,
                    message.thread_id,
                    lambda record: _set_thread_summary(
                        record,
                        thread_id,
                        assistant_preview=assistant_preview,
                        last_used_at=now_iso(),
                        workspace_path=record.workspace_path,
                        rollout_path=record.rollout_path,
                    ),
                )
        turn_handle_id = turn_handle.turn_id if turn_handle else None
        if is_interrupt_status(result.status):
            response = _compose_interrupt_response(response)
            if (
                runtime.interrupt_message_id is not None
                and runtime.interrupt_turn_id == turn_handle_id
            ):
                await self._edit_message_text(
                    message.chat_id,
                    runtime.interrupt_message_id,
                    "Interrupted.",
                )
                runtime.interrupt_message_id = None
                runtime.interrupt_turn_id = None
            runtime.interrupt_requested = False
        elif runtime.interrupt_turn_id == turn_handle_id:
            if runtime.interrupt_message_id is not None:
                await self._edit_message_text(
                    message.chat_id,
                    runtime.interrupt_message_id,
                    "Interrupt requested; turn completed.",
                )
            runtime.interrupt_message_id = None
            runtime.interrupt_turn_id = None
            runtime.interrupt_requested = False
        log_event(
            self._logger,
            logging.INFO,
            "telegram.review.completed",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            turn_id=turn_handle.turn_id if turn_handle else None,
            agent_message_count=len(result.agent_messages),
            error_count=len(result.errors),
        )
        turn_id = turn_handle.turn_id if turn_handle else None
        token_usage = self._token_usage_by_turn.get(turn_id) if turn_id else None
        metrics = self._format_turn_metrics_text(token_usage, turn_elapsed_seconds)
        metrics_mode = self._metrics_mode()
        response_text = response
        if metrics and metrics_mode == "append_to_response":
            response_text = f"{response_text}\n\n{metrics}"
        response_sent = await self._deliver_turn_response(
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            reply_to=message.message_id,
            placeholder_id=placeholder_id,
            response=response_text,
        )
        placeholder_handled = False
        if metrics and metrics_mode == "separate":
            await self._send_turn_metrics(
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                reply_to=message.message_id,
                elapsed_seconds=turn_elapsed_seconds,
                token_usage=token_usage,
            )
        elif metrics and metrics_mode == "append_to_progress" and response_sent:
            placeholder_handled = await self._append_metrics_to_placeholder(
                message.chat_id, placeholder_id, metrics
            )
        if turn_id:
            self._token_usage_by_turn.pop(turn_id, None)
        if response_sent:
            if not placeholder_handled:
                await self._delete_message(message.chat_id, placeholder_id)
        await self._flush_outbox_files(
            record,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _start_opencode_review(
        self,
        message: TelegramMessage,
        runtime: Any,
        *,
        record: TelegramTopicRecord,
        thread_id: str,
        target: dict[str, Any],
        delivery: str,
    ) -> None:
        supervisor = getattr(self, "_opencode_supervisor", None)
        if supervisor is None:
            await self._send_message(
                message.chat_id,
                "OpenCode backend unavailable; install opencode or switch to /agent codex.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        workspace_root = self._canonical_workspace_root(record.workspace_path)
        if workspace_root is None:
            await self._send_message(
                message.chat_id,
                "Workspace unavailable.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        try:
            opencode_client = await supervisor.get_client(workspace_root)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.opencode.client.failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                "OpenCode backend unavailable.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        review_session_id = thread_id
        if delivery == "detached":
            try:
                session = await opencode_client.create_session(
                    directory=str(workspace_root)
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.opencode.session.failed",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    exc=exc,
                )
                await self._send_message(
                    message.chat_id,
                    "Failed to start a new OpenCode thread.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            review_session_id = extract_session_id(session, allow_fallback_id=True)
            if not review_session_id:
                await self._send_message(
                    message.chat_id,
                    "Failed to start a new OpenCode thread.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return

            def apply(record: "TelegramTopicRecord") -> None:
                if review_session_id in record.thread_ids:
                    record.thread_ids.remove(review_session_id)
                record.thread_ids.insert(0, review_session_id)
                if len(record.thread_ids) > MAX_TOPIC_THREAD_HISTORY:
                    record.thread_ids = record.thread_ids[:MAX_TOPIC_THREAD_HISTORY]
                _set_thread_summary(
                    record,
                    review_session_id,
                    last_used_at=now_iso(),
                    workspace_path=record.workspace_path,
                    rollout_path=record.rollout_path,
                )

            await self._router.update_topic(message.chat_id, message.thread_id, apply)
        agent = self._effective_agent(record)
        log_event(
            self._logger,
            logging.INFO,
            "telegram.review.starting",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            codex_thread_id=review_session_id,
            delivery=delivery,
            target=target.get("type"),
            agent=agent,
        )
        approval_policy, _sandbox_policy = self._effective_policies(record)
        permission_policy = map_approval_policy_to_permission(
            approval_policy, default=PERMISSION_ALLOW
        )
        review_args = _opencode_review_arguments(target)
        turn_key: Optional[TurnKey] = None
        placeholder_id: Optional[int] = None
        turn_started_at: Optional[float] = None
        turn_elapsed_seconds: Optional[float] = None
        turn_id: Optional[str] = None
        output_result = None
        queued = False
        placeholder_text = PLACEHOLDER_TEXT
        try:
            turn_semaphore = self._ensure_turn_semaphore()
            queued = turn_semaphore.locked()
            if queued:
                placeholder_text = QUEUED_PLACEHOLDER_TEXT
            placeholder_id = await self._send_placeholder(
                message.chat_id,
                thread_id=message.thread_id,
                reply_to=message.message_id,
                text=placeholder_text,
                reply_markup=self._interrupt_keyboard(),
            )
            queue_started_at = time.monotonic()
            acquired = await self._await_turn_slot(
                turn_semaphore,
                runtime,
                message=message,
                placeholder_id=placeholder_id,
                queued=queued,
            )
            if not acquired:
                runtime.interrupt_requested = False
                return
            try:
                queue_wait_ms = int((time.monotonic() - queue_started_at) * 1000)
                log_event(
                    self._logger,
                    logging.INFO,
                    "telegram.review.queue_wait",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    codex_thread_id=review_session_id,
                    queue_wait_ms=queue_wait_ms,
                    queued=queued,
                    max_parallel_turns=self._config.concurrency.max_parallel_turns,
                    per_topic_queue=self._config.concurrency.per_topic_queue,
                )
                if (
                    queued
                    and placeholder_id is not None
                    and placeholder_text != PLACEHOLDER_TEXT
                ):
                    await self._edit_message_text(
                        message.chat_id,
                        placeholder_id,
                        PLACEHOLDER_TEXT,
                    )
                opencode_turn_started = False
                try:
                    await supervisor.mark_turn_started(workspace_root)
                    opencode_turn_started = True
                    model_payload = split_model_id(record.model)
                    missing_env = await opencode_missing_env(
                        opencode_client,
                        str(workspace_root),
                        model_payload,
                    )
                    if missing_env:
                        provider_id = (
                            model_payload.get("providerID") if model_payload else None
                        )
                        failure_message = (
                            "OpenCode provider "
                            f"{provider_id or 'selected'} requires env vars: "
                            f"{', '.join(missing_env)}. "
                            "Set them or switch models."
                        )
                        response_sent = await self._deliver_turn_response(
                            chat_id=message.chat_id,
                            thread_id=message.thread_id,
                            reply_to=message.message_id,
                            placeholder_id=placeholder_id,
                            response=failure_message,
                        )
                        if response_sent:
                            await self._delete_message(message.chat_id, placeholder_id)
                        return
                    turn_started_at = time.monotonic()
                    turn_id = build_turn_id(review_session_id)
                    self._token_usage_by_thread.pop(review_session_id, None)
                    runtime.current_turn_id = turn_id
                    runtime.current_turn_key = (review_session_id, turn_id)
                    topic_key = await self._resolve_topic_key(
                        message.chat_id, message.thread_id
                    )
                    ctx = TurnContext(
                        topic_key=topic_key,
                        chat_id=message.chat_id,
                        thread_id=message.thread_id,
                        codex_thread_id=review_session_id,
                        reply_to_message_id=message.message_id,
                        placeholder_message_id=placeholder_id,
                    )
                    turn_key = self._turn_key(review_session_id, turn_id)
                    if turn_key is None or not self._register_turn_context(
                        turn_key, turn_id, ctx
                    ):
                        runtime.current_turn_id = None
                        runtime.current_turn_key = None
                        runtime.interrupt_requested = False
                        await self._send_message(
                            message.chat_id,
                            "Turn collision detected; please retry.",
                            thread_id=message.thread_id,
                            reply_to=message.message_id,
                        )
                        if placeholder_id is not None:
                            await self._delete_message(message.chat_id, placeholder_id)
                        return
                    await self._start_turn_progress(
                        turn_key,
                        ctx=ctx,
                        agent="opencode",
                        model=record.model,
                        label="review",
                    )

                    async def _permission_handler(
                        request_id: str, props: dict[str, Any]
                    ) -> str:
                        if permission_policy != PERMISSION_ASK:
                            return "reject"
                        prompt = format_permission_prompt(props)
                        decision = await self._handle_approval_request(
                            {
                                "id": request_id,
                                "method": "opencode/permission/requestApproval",
                                "params": {
                                    "turnId": turn_id,
                                    "threadId": review_session_id,
                                    "prompt": prompt,
                                },
                            }
                        )
                        return decision

                    abort_requested = False

                    async def _abort_opencode() -> None:
                        try:
                            await opencode_client.abort(review_session_id)
                        except Exception:
                            pass

                    def _should_stop() -> bool:
                        nonlocal abort_requested
                        if runtime.interrupt_requested and not abort_requested:
                            abort_requested = True
                            asyncio.create_task(_abort_opencode())
                        return runtime.interrupt_requested

                    reasoning_buffers: dict[str, str] = {}
                    watched_session_ids = {review_session_id}
                    subagent_labels: dict[str, str] = {}
                    opencode_context_window: Optional[int] = None
                    context_window_resolved = False

                    async def _handle_opencode_part(
                        part_type: str,
                        part: dict[str, Any],
                        delta_text: Optional[str],
                    ) -> None:
                        nonlocal opencode_context_window
                        nonlocal context_window_resolved
                        if turn_key is None:
                            return
                        tracker = self._turn_progress_trackers.get(turn_key)
                        if tracker is None:
                            return
                        session_id = None
                        for key in ("sessionID", "sessionId", "session_id"):
                            value = part.get(key)
                            if isinstance(value, str) and value:
                                session_id = value
                                break
                        if not session_id:
                            session_id = review_session_id
                        is_primary_session = session_id == review_session_id
                        subagent_label = subagent_labels.get(session_id)
                        if part_type == "reasoning":
                            part_id = (
                                part.get("id") or part.get("partId") or "reasoning"
                            )
                            buffer_key = f"{session_id}:{part_id}"
                            buffer = reasoning_buffers.get(buffer_key, "")
                            if delta_text:
                                buffer = f"{buffer}{delta_text}"
                            else:
                                raw_text = part.get("text")
                                if isinstance(raw_text, str) and raw_text:
                                    buffer = raw_text
                            if buffer:
                                reasoning_buffers[buffer_key] = buffer
                                preview = _compact_preview(buffer, limit=240)
                                if is_primary_session:
                                    tracker.note_thinking(preview)
                                else:
                                    if not subagent_label:
                                        subagent_label = "@subagent"
                                        subagent_labels.setdefault(
                                            session_id, subagent_label
                                        )
                                    if not tracker.update_action_by_item_id(
                                        buffer_key,
                                        preview,
                                        "update",
                                        label="thinking",
                                        subagent_label=subagent_label,
                                    ):
                                        tracker.add_action(
                                            "thinking",
                                            preview,
                                            "update",
                                            item_id=buffer_key,
                                            subagent_label=subagent_label,
                                        )
                        elif part_type == "tool":
                            tool_id = part.get("callID") or part.get("id")
                            tool_name = part.get("tool") or part.get("name") or "tool"
                            status = None
                            state = part.get("state")
                            if isinstance(state, dict):
                                status = state.get("status")
                            label = (
                                f"{tool_name} ({status})"
                                if isinstance(status, str) and status
                                else str(tool_name)
                            )
                            if (
                                is_primary_session
                                and isinstance(tool_name, str)
                                and tool_name == "task"
                                and isinstance(state, dict)
                            ):
                                metadata = state.get("metadata")
                                if isinstance(metadata, dict):
                                    child_session_id = metadata.get(
                                        "sessionId"
                                    ) or metadata.get("sessionID")
                                    if (
                                        isinstance(child_session_id, str)
                                        and child_session_id
                                    ):
                                        watched_session_ids.add(child_session_id)
                                        child_label = None
                                        input_payload = state.get("input")
                                        if isinstance(input_payload, dict):
                                            child_label = input_payload.get(
                                                "subagent_type"
                                            ) or input_payload.get("subagentType")
                                        if (
                                            isinstance(child_label, str)
                                            and child_label.strip()
                                        ):
                                            child_label = child_label.strip()
                                            if not child_label.startswith("@"):
                                                child_label = f"@{child_label}"
                                            subagent_labels.setdefault(
                                                child_session_id, child_label
                                            )
                                        else:
                                            subagent_labels.setdefault(
                                                child_session_id, "@subagent"
                                            )
                                detail_parts: list[str] = []
                                title = state.get("title")
                                if isinstance(title, str) and title.strip():
                                    detail_parts.append(title.strip())
                                input_payload = state.get("input")
                                if isinstance(input_payload, dict):
                                    description = input_payload.get("description")
                                    if (
                                        isinstance(description, str)
                                        and description.strip()
                                    ):
                                        detail_parts.append(description.strip())
                                summary = None
                                if isinstance(metadata, dict):
                                    summary = metadata.get("summary")
                                if isinstance(summary, str) and summary.strip():
                                    detail_parts.append(summary.strip())
                                if detail_parts:
                                    seen: set[str] = set()
                                    unique_parts = [
                                        part_text
                                        for part_text in detail_parts
                                        if part_text not in seen
                                        and not seen.add(part_text)
                                    ]
                                    detail_text = " / ".join(unique_parts)
                                    label = f"{label} - {_compact_preview(detail_text, limit=160)}"
                            mapped_status = "update"
                            if isinstance(status, str):
                                status_lower = status.lower()
                                if status_lower in ("completed", "done", "success"):
                                    mapped_status = "done"
                                elif status_lower in ("error", "failed", "fail"):
                                    mapped_status = "fail"
                                elif status_lower in ("pending", "running"):
                                    mapped_status = "running"
                            scoped_tool_id = (
                                f"{session_id}:{tool_id}"
                                if isinstance(tool_id, str) and tool_id
                                else None
                            )
                            if is_primary_session:
                                if not tracker.update_action_by_item_id(
                                    scoped_tool_id,
                                    label,
                                    mapped_status,
                                    label="tool",
                                ):
                                    tracker.add_action(
                                        "tool",
                                        label,
                                        mapped_status,
                                        item_id=scoped_tool_id,
                                    )
                            else:
                                if not subagent_label:
                                    subagent_label = "@subagent"
                                    subagent_labels.setdefault(
                                        session_id, subagent_label
                                    )
                                if not tracker.update_action_by_item_id(
                                    scoped_tool_id,
                                    label,
                                    mapped_status,
                                    label=subagent_label,
                                ):
                                    tracker.add_action(
                                        subagent_label,
                                        label,
                                        mapped_status,
                                        item_id=scoped_tool_id,
                                    )
                        elif part_type == "patch":
                            patch_id = part.get("id") or part.get("hash")
                            files = part.get("files")
                            scoped_patch_id = (
                                f"{session_id}:{patch_id}"
                                if isinstance(patch_id, str) and patch_id
                                else None
                            )
                            if isinstance(files, list) and files:
                                summary = ", ".join(str(file) for file in files)
                                if not tracker.update_action_by_item_id(
                                    scoped_patch_id, summary, "done", label="files"
                                ):
                                    tracker.add_action(
                                        "files",
                                        summary,
                                        "done",
                                        item_id=scoped_patch_id,
                                    )
                            else:
                                if not tracker.update_action_by_item_id(
                                    scoped_patch_id, "Patch", "done", label="files"
                                ):
                                    tracker.add_action(
                                        "files",
                                        "Patch",
                                        "done",
                                        item_id=scoped_patch_id,
                                    )
                        elif part_type == "agent":
                            agent_name = part.get("name") or "agent"
                            tracker.add_action("agent", str(agent_name), "done")
                        elif part_type == "step-start":
                            tracker.add_action("step", "started", "update")
                        elif part_type == "step-finish":
                            reason = part.get("reason") or "finished"
                            tracker.add_action("step", str(reason), "done")
                        elif part_type == "usage":
                            token_usage = (
                                _build_opencode_token_usage(part)
                                if isinstance(part, dict)
                                else None
                            )
                            if token_usage:
                                if is_primary_session:
                                    if (
                                        "modelContextWindow" not in token_usage
                                        and not context_window_resolved
                                    ):
                                        opencode_context_window = await self._resolve_opencode_model_context_window(
                                            opencode_client,
                                            workspace_root,
                                            model_payload,
                                        )
                                        context_window_resolved = True
                                    if (
                                        "modelContextWindow" not in token_usage
                                        and isinstance(opencode_context_window, int)
                                        and opencode_context_window > 0
                                    ):
                                        token_usage["modelContextWindow"] = (
                                            opencode_context_window
                                        )
                                    self._cache_token_usage(
                                        token_usage,
                                        turn_id=turn_id,
                                        thread_id=review_session_id,
                                    )
                                    await self._note_progress_context_usage(
                                        token_usage,
                                        turn_id=turn_id,
                                        thread_id=review_session_id,
                                    )
                        await self._schedule_progress_edit(turn_key)

                    ready_event = asyncio.Event()
                    output_task = asyncio.create_task(
                        collect_opencode_output(
                            opencode_client,
                            session_id=review_session_id,
                            workspace_path=str(workspace_root),
                            progress_session_ids=watched_session_ids,
                            permission_policy=permission_policy,
                            permission_handler=(
                                _permission_handler
                                if permission_policy == PERMISSION_ASK
                                else None
                            ),
                            should_stop=_should_stop,
                            part_handler=_handle_opencode_part,
                            ready_event=ready_event,
                        )
                    )
                    with suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(ready_event.wait(), timeout=2.0)
                    command_task = asyncio.create_task(
                        opencode_client.send_command(
                            review_session_id,
                            command="review",
                            arguments=review_args,
                            model=record.model,
                        )
                    )
                    try:
                        await command_task
                    except Exception as exc:
                        output_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await output_task
                        raise exc
                    timeout_task = asyncio.create_task(
                        asyncio.sleep(OPENCODE_TURN_TIMEOUT_SECONDS)
                    )
                    done, _pending = await asyncio.wait(
                        {output_task, timeout_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    if timeout_task in done:
                        runtime.interrupt_requested = True
                        await _abort_opencode()
                        output_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await output_task
                        if turn_started_at is not None:
                            turn_elapsed_seconds = time.monotonic() - turn_started_at
                        failure_message = "OpenCode review timed out."
                        response_sent = await self._deliver_turn_response(
                            chat_id=message.chat_id,
                            thread_id=message.thread_id,
                            reply_to=message.message_id,
                            placeholder_id=placeholder_id,
                            response=failure_message,
                        )
                        if response_sent:
                            await self._delete_message(message.chat_id, placeholder_id)
                        return
                    timeout_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await timeout_task
                    output_result = await output_task
                    if turn_started_at is not None:
                        turn_elapsed_seconds = time.monotonic() - turn_started_at
                finally:
                    if opencode_turn_started:
                        await supervisor.mark_turn_finished(workspace_root)
            finally:
                turn_semaphore.release()
        except Exception as exc:
            if turn_key is not None:
                self._turn_contexts.pop(turn_key, None)
            runtime.current_turn_id = None
            runtime.current_turn_key = None
            runtime.interrupt_requested = False
            failure_message = (
                _format_opencode_exception(exc)
                or "OpenCode review failed; check logs for details."
            )
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.review.failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            response_sent = await self._deliver_turn_response(
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                reply_to=message.message_id,
                placeholder_id=placeholder_id,
                response=_with_conversation_id(
                    failure_message,
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                ),
            )
            if response_sent:
                await self._delete_message(message.chat_id, placeholder_id)
            return
        finally:
            if turn_key is not None:
                self._turn_contexts.pop(turn_key, None)
                self._clear_thinking_preview(turn_key)
                self._clear_turn_progress(turn_key)
            runtime.current_turn_id = None
            runtime.current_turn_key = None
            runtime.interrupt_requested = False
        if output_result is None:
            return
        output = output_result.text
        if output_result.error:
            failure_message = f"OpenCode review failed: {output_result.error}"
            response_sent = await self._deliver_turn_response(
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                reply_to=message.message_id,
                placeholder_id=placeholder_id,
                response=failure_message,
            )
            if response_sent:
                await self._delete_message(message.chat_id, placeholder_id)
            return
        if output:
            assistant_preview = _preview_from_text(
                output, RESUME_PREVIEW_ASSISTANT_LIMIT
            )
            if assistant_preview:
                await self._router.update_topic(
                    message.chat_id,
                    message.thread_id,
                    lambda record: _set_thread_summary(
                        record,
                        review_session_id,
                        assistant_preview=assistant_preview,
                        last_used_at=now_iso(),
                        workspace_path=record.workspace_path,
                        rollout_path=record.rollout_path,
                    ),
                )
        log_event(
            self._logger,
            logging.INFO,
            "telegram.review.completed",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            turn_id=turn_id,
        )
        token_usage = self._token_usage_by_turn.get(turn_id) if turn_id else None
        metrics = self._format_turn_metrics_text(token_usage, turn_elapsed_seconds)
        metrics_mode = self._metrics_mode()
        response_text = output or "No response."
        if metrics and metrics_mode == "append_to_response":
            response_text = f"{response_text}\n\n{metrics}"
        response_sent = await self._deliver_turn_response(
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            reply_to=message.message_id,
            placeholder_id=placeholder_id,
            response=response_text,
        )
        placeholder_handled = False
        if metrics and metrics_mode == "separate":
            await self._send_turn_metrics(
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                reply_to=message.message_id,
                elapsed_seconds=turn_elapsed_seconds,
                token_usage=token_usage,
            )
        elif metrics and metrics_mode == "append_to_progress" and response_sent:
            placeholder_handled = await self._append_metrics_to_placeholder(
                message.chat_id, placeholder_id, metrics
            )
        if turn_id:
            self._token_usage_by_turn.pop(turn_id, None)
        if response_sent:
            if not placeholder_handled:
                await self._delete_message(message.chat_id, placeholder_id)
        await self._flush_outbox_files(
            record,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _start_review(
        self,
        message: TelegramMessage,
        runtime: Any,
        *,
        record: TelegramTopicRecord,
        thread_id: str,
        target: dict[str, Any],
        delivery: str,
    ) -> None:
        agent = self._effective_agent(record)
        if agent == "opencode":
            await self._start_opencode_review(
                message,
                runtime,
                record=record,
                thread_id=thread_id,
                target=target,
                delivery=delivery,
            )
            return
        await self._start_codex_review(
            message,
            runtime,
            record=record,
            thread_id=thread_id,
            target=target,
            delivery=delivery,
        )

    async def _handle_review(
        self, message: TelegramMessage, args: str, runtime: Any
    ) -> None:
        record = await self._require_bound_record(message)
        if not record:
            return
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        raw_args = args.strip()
        delivery = "inline"
        if raw_args:
            detached_pattern = r"(^|\s)(--detached|detached)(?=\s|$)"
            if re.search(detached_pattern, raw_args, flags=re.IGNORECASE):
                delivery = "detached"
                raw_args = re.sub(detached_pattern, " ", raw_args, flags=re.IGNORECASE)
                raw_args = raw_args.strip()
        token, remainder = _consume_raw_token(raw_args)
        target: dict[str, Any] = {"type": "uncommittedChanges"}
        if token:
            keyword = token.lower()
            if keyword == "base":
                argv = self._parse_command_args(raw_args)
                if len(argv) < 2:
                    await self._send_message(
                        message.chat_id,
                        "Usage: /review base <branch>",
                        thread_id=message.thread_id,
                        reply_to=message.message_id,
                    )
                    return
                target = {"type": "baseBranch", "branch": argv[1]}
            elif keyword == "pr":
                argv = self._parse_command_args(raw_args)
                branch = argv[1] if len(argv) > 1 else "main"
                target = {"type": "baseBranch", "branch": branch}
            elif keyword == "commit":
                argv = self._parse_command_args(raw_args)
                if len(argv) < 2:
                    await self._prompt_review_commit_picker(
                        message, record, delivery=delivery
                    )
                    return
                target = {"type": "commit", "sha": argv[1]}
            elif keyword == "custom":
                instructions = remainder
                if instructions.startswith((" ", "\t")):
                    instructions = instructions[1:]
                if not instructions.strip():
                    prompt_text = (
                        "Reply with review instructions (next message will be used)."
                    )
                    cancel_keyboard = build_inline_keyboard(
                        [
                            [
                                InlineButton(
                                    "Cancel",
                                    encode_cancel_callback("review-custom"),
                                )
                            ]
                        ]
                    )
                    payload_text, parse_mode = self._prepare_message(prompt_text)
                    response = await self._bot.send_message(
                        message.chat_id,
                        payload_text,
                        message_thread_id=message.thread_id,
                        reply_to_message_id=message.message_id,
                        reply_markup=cancel_keyboard,
                        parse_mode=parse_mode,
                    )
                    prompt_message_id = (
                        response.get("message_id")
                        if isinstance(response, dict)
                        else None
                    )
                    self._pending_review_custom[key] = {
                        "delivery": delivery,
                        "message_id": prompt_message_id,
                        "prompt_text": prompt_text,
                    }
                    self._touch_cache_timestamp("pending_review_custom", key)
                    return
                target = {"type": "custom", "instructions": instructions}
            else:
                instructions = raw_args.strip()
                if instructions:
                    target = {"type": "custom", "instructions": instructions}
        thread_id = await self._ensure_thread_id(message, record)
        if not thread_id:
            return
        await self._start_review(
            message,
            runtime,
            record=record,
            thread_id=thread_id,
            target=target,
            delivery=delivery,
        )

    def _resolve_pr_flow_repo_id(self, record: "TelegramTopicRecord") -> Optional[str]:
        if record.repo_id:
            return record.repo_id
        if not self._hub_root or not self._manifest_path or not record.workspace_path:
            return None
        try:
            manifest = load_manifest(self._manifest_path, self._hub_root)
        except Exception:
            return None
        try:
            workspace_path = canonicalize_path(Path(record.workspace_path))
        except Exception:
            return None
        for repo in manifest.repos:
            repo_path = canonicalize_path(self._hub_root / repo.path)
            if repo_path == workspace_path:
                return repo.id
        return None

    def _pr_flow_api_base(
        self, record: "TelegramTopicRecord"
    ) -> tuple[Optional[str], dict[str, str]]:
        headers: dict[str, str] = {}
        if self._hub_root is not None:
            try:
                hub_config = load_hub_config(self._hub_root)
            except Exception:
                return None, headers
            host = hub_config.server_host
            port = hub_config.server_port
            base_path = hub_config.server_base_path
            auth_env = hub_config.server_auth_token_env
            repo_id = self._resolve_pr_flow_repo_id(record)
            if not repo_id:
                return None, headers
            repo_prefix = f"/repos/{repo_id}"
        else:
            if not record.workspace_path:
                return None, headers
            try:
                repo_config = load_repo_config(
                    Path(record.workspace_path), hub_path=None
                )
            except Exception:
                return None, headers
            host = repo_config.server_host
            port = repo_config.server_port
            base_path = repo_config.server_base_path
            auth_env = repo_config.server_auth_token_env
            repo_prefix = ""
        if isinstance(auth_env, str) and auth_env:
            token = getenv(auth_env)
            if token:
                headers["Authorization"] = f"Bearer {token}"
        if not host:
            return None, headers
        if host.startswith("http://") or host.startswith("https://"):
            base = host.rstrip("/")
        else:
            base = f"http://{host}:{int(port)}"
        base_path = (base_path or "").strip("/")
        if base_path:
            base = f"{base}/{base_path}"
        return f"{base}{repo_prefix}", headers

    async def _pr_flow_request(
        self,
        record: "TelegramTopicRecord",
        *,
        method: str,
        path: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        base, headers = self._pr_flow_api_base(record)
        if not base:
            raise RuntimeError(
                "PR flow cannot start: repo server base URL could not be resolved for this chat/topic."
            )
        url = f"{base}{path}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            res = await client.request(method, url, json=payload, headers=headers)
            res.raise_for_status()
            data = res.json()
            if isinstance(data, dict):
                return data
            return {"status": "ok", "flow": data}

    def _parse_pr_flags(self, argv: list[str]) -> tuple[Optional[str], dict[str, Any]]:
        ref: Optional[str] = None
        flags: dict[str, Any] = {}
        idx = 0
        while idx < len(argv):
            token = argv[idx]
            if token.startswith("--"):
                if token == "--draft":
                    flags["draft"] = True
                    idx += 1
                    continue
                if token == "--ready":
                    flags["draft"] = False
                    idx += 1
                    continue
                if token == "--base" and idx + 1 < len(argv):
                    flags["base_branch"] = argv[idx + 1]
                    idx += 2
                    continue
                if token == "--until" and idx + 1 < len(argv):
                    until = argv[idx + 1].strip().lower()
                    if until in ("minor", "minor_only"):
                        flags["stop_condition"] = "minor_only"
                    elif until in ("clean", "no_issues"):
                        flags["stop_condition"] = "no_issues"
                    idx += 2
                    continue
                if token in ("--max-cycles", "--max_cycles") and idx + 1 < len(argv):
                    try:
                        flags["max_cycles"] = int(argv[idx + 1])
                    except ValueError:
                        pass
                    idx += 2
                    continue
                if token in ("--max-runs", "--max_runs") and idx + 1 < len(argv):
                    try:
                        flags["max_implementation_runs"] = int(argv[idx + 1])
                    except ValueError:
                        pass
                    idx += 2
                    continue
                if token in ("--timeout", "--timeout-seconds") and idx + 1 < len(argv):
                    try:
                        flags["max_wallclock_seconds"] = int(argv[idx + 1])
                    except ValueError:
                        pass
                    idx += 2
                    continue
                idx += 1
                continue
            if ref is None:
                ref = token
            idx += 1
        return ref, flags

    def _format_pr_flow_status(self, flow: dict[str, Any]) -> str:
        status = flow.get("status") or "unknown"
        step = flow.get("step") or "unknown"
        cycle = flow.get("cycle") or 0
        pr_url = flow.get("pr_url") or ""
        lines = [f"PR flow: {status} (step: {step}, cycle: {cycle})"]
        if pr_url:
            lines.append(f"PR: {pr_url}")
        return "\n".join(lines)

    async def _handle_github_issue_url(
        self, message: TelegramMessage, key: str, slug: str, number: int
    ) -> None:
        if key is None:
            return

        record = await self._router.get_topic(key)
        if record is None or not record.workspace_path:
            await self._send_message(
                message.chat_id,
                self._with_conversation_id(
                    "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        try:
            from pathlib import Path

            service = GitHubService(Path(record.workspace_path), self._raw_config)
            issue_ref = f"{slug}#{number}"
            service.validate_issue_same_repo(issue_ref)
        except GitHubError as exc:
            await self._send_message(
                message.chat_id,
                str(exc),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        await self._offer_pr_flow_start(message, record, slug, number)

    async def _offer_pr_flow_start(
        self,
        message: TelegramMessage,
        record: "TelegramTopicRecord",
        slug: str,
        number: int,
    ) -> None:
        from ..adapter import (
            InlineButton,
            build_inline_keyboard,
            encode_cancel_callback,
            encode_pr_flow_start_callback,
        )

        keyboard = build_inline_keyboard(
            [
                [
                    InlineButton(
                        f"Create PR for #{number}",
                        encode_pr_flow_start_callback(slug, number),
                    ),
                    InlineButton(
                        "Cancel",
                        encode_cancel_callback("pr_flow_offer"),
                    ),
                ]
            ]
        )
        await self._send_message(
            message.chat_id,
            f"Detected GitHub issue: {slug}#{number}\n"
            f"Start PR flow to create a PR?",
            thread_id=message.thread_id,
            reply_to=message.message_id,
            reply_markup=keyboard,
        )

    async def _handle_pr_flow_start_callback(
        self,
        key: str,
        callback: TelegramCallbackQuery,
        parsed: PrFlowStartCallback,
    ) -> None:
        from ..adapter import TelegramMessage

        await self._answer_callback(callback)
        record = await self._router.get_topic(key)
        if record is None or not record.workspace_path:
            return

        issue_ref = f"{parsed.slug}#{parsed.number}"
        payload = {"mode": "issue", "issue": issue_ref}
        payload["source"] = "telegram"
        source_meta: dict[str, Any] = {}
        if callback.chat_id is not None:
            source_meta["chat_id"] = callback.chat_id
        if callback.thread_id is not None:
            source_meta["thread_id"] = callback.thread_id
        if source_meta:
            payload["source_meta"] = source_meta

        message = TelegramMessage(
            update_id=callback.update_id,
            message_id=callback.message_id or 0,
            chat_id=callback.chat_id or 0,
            thread_id=callback.thread_id,
            from_user_id=callback.from_user_id,
            text="",
            date=None,
            is_topic_message=False,
        )

        try:
            data = await self._pr_flow_request(
                record,
                method="POST",
                path="/api/github/pr_flow/start",
                payload=payload,
            )
            flow = data.get("flow") if isinstance(data, dict) else data
        except Exception as exc:
            detail = _format_httpx_exception(exc) or str(exc)
            await self._send_message(
                message.chat_id,
                f"PR flow error: {detail}",
                thread_id=message.thread_id,
                reply_to=callback.message_id,
            )
            return
        await self._send_message(
            message.chat_id,
            self._format_pr_flow_status(flow),
            thread_id=message.thread_id,
            reply_to=callback.message_id,
        )

    async def _handle_pr(
        self, message: TelegramMessage, args: str, runtime: Any
    ) -> None:
        record = await self._require_bound_record(message)
        if not record:
            return
        argv = self._parse_command_args(args)
        if not argv:
            await self._send_message(
                message.chat_id,
                "Usage: /pr start <issueRef> | /pr fix <prRef> | /pr status | /pr stop | /pr resume | /pr collect",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        command = argv[0].lower()
        if command == "status":
            try:
                data = await self._pr_flow_request(
                    record, method="GET", path="/api/github/pr_flow/status"
                )
                flow = data.get("flow") if isinstance(data, dict) else data
            except Exception as exc:
                detail = _format_httpx_exception(exc) or str(exc)
                await self._send_message(
                    message.chat_id,
                    f"PR flow error: {detail}",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            await self._send_message(
                message.chat_id,
                self._format_pr_flow_status(flow),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if command == "stop":
            try:
                data = await self._pr_flow_request(
                    record, method="POST", path="/api/github/pr_flow/stop", payload={}
                )
                flow = data.get("flow") if isinstance(data, dict) else data
            except Exception as exc:
                detail = _format_httpx_exception(exc) or str(exc)
                await self._send_message(
                    message.chat_id,
                    f"PR flow error: {detail}",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            await self._send_message(
                message.chat_id,
                self._format_pr_flow_status(flow),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if command == "resume":
            try:
                data = await self._pr_flow_request(
                    record, method="POST", path="/api/github/pr_flow/resume", payload={}
                )
                flow = data.get("flow") if isinstance(data, dict) else data
            except Exception as exc:
                detail = _format_httpx_exception(exc) or str(exc)
                await self._send_message(
                    message.chat_id,
                    f"PR flow error: {detail}",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            await self._send_message(
                message.chat_id,
                self._format_pr_flow_status(flow),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if command == "collect":
            try:
                data = await self._pr_flow_request(
                    record,
                    method="POST",
                    path="/api/github/pr_flow/collect",
                    payload={},
                )
                flow = data.get("flow") if isinstance(data, dict) else data
            except Exception as exc:
                detail = _format_httpx_exception(exc) or str(exc)
                await self._send_message(
                    message.chat_id,
                    f"PR flow error: {detail}",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            await self._send_message(
                message.chat_id,
                self._format_pr_flow_status(flow),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if command in ("start", "implement"):
            ref, flags = self._parse_pr_flags(argv[1:])
            if not ref:
                gh = GitHubService(Path(record.workspace_path))
                issues = await asyncio.to_thread(gh.list_open_issues, limit=5)
                if issues:
                    lines = ["Open issues:"]
                    for issue in issues:
                        num = issue.get("number")
                        title = issue.get("title") or ""
                        lines.append(f"- #{num} {title}".strip())
                    lines.append("Use /pr start <issueRef> to begin.")
                    await self._send_message(
                        message.chat_id,
                        "\n".join(lines),
                        thread_id=message.thread_id,
                        reply_to=message.message_id,
                    )
                    return
                await self._send_message(
                    message.chat_id,
                    "Usage: /pr start <issueRef>",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            payload = {"mode": "issue", "issue": ref, **flags}
            payload["source"] = "telegram"
            payload["source_meta"] = {
                "chat_id": message.chat_id,
                "thread_id": message.thread_id,
            }
            try:
                data = await self._pr_flow_request(
                    record,
                    method="POST",
                    path="/api/github/pr_flow/start",
                    payload=payload,
                )
                flow = data.get("flow") if isinstance(data, dict) else data
            except Exception as exc:
                detail = _format_httpx_exception(exc) or str(exc)
                await self._send_message(
                    message.chat_id,
                    f"PR flow error: {detail}",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            await self._send_message(
                message.chat_id,
                self._format_pr_flow_status(flow),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if command in ("fix", "pr"):
            ref, flags = self._parse_pr_flags(argv[1:])
            if not ref:
                gh = GitHubService(Path(record.workspace_path))
                prs = await asyncio.to_thread(gh.list_open_prs, limit=5)
                if prs:
                    lines = ["Open PRs:"]
                    for pr in prs:
                        num = pr.get("number")
                        title = pr.get("title") or ""
                        lines.append(f"- #{num} {title}".strip())
                    lines.append("Use /pr fix <prRef> to begin.")
                    await self._send_message(
                        message.chat_id,
                        "\n".join(lines),
                        thread_id=message.thread_id,
                        reply_to=message.message_id,
                    )
                    return
                await self._send_message(
                    message.chat_id,
                    "Usage: /pr fix <prRef>",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            payload = {"mode": "pr", "pr": ref, **flags}
            payload["source"] = "telegram"
            payload["source_meta"] = {
                "chat_id": message.chat_id,
                "thread_id": message.thread_id,
            }
            try:
                data = await self._pr_flow_request(
                    record,
                    method="POST",
                    path="/api/github/pr_flow/start",
                    payload=payload,
                )
                flow = data.get("flow") if isinstance(data, dict) else data
            except Exception as exc:
                detail = _format_httpx_exception(exc) or str(exc)
                await self._send_message(
                    message.chat_id,
                    f"PR flow error: {detail}",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            await self._send_message(
                message.chat_id,
                self._format_pr_flow_status(flow),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        await self._send_message(
            message.chat_id,
            "Unknown /pr command. Use /pr start|fix|status|stop|resume|collect.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _prompt_review_commit_picker(
        self,
        message: TelegramMessage,
        record: TelegramTopicRecord,
        *,
        delivery: str,
    ) -> None:
        commits = await self._list_recent_commits(record)
        if not commits:
            await self._send_message(
                message.chat_id,
                "No recent commits found.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        items: list[tuple[str, str]] = []
        subjects: dict[str, str] = {}
        for sha, subject in commits:
            label = _format_review_commit_label(sha, subject)
            items.append((sha, label))
            if subject:
                subjects[sha] = subject
        state = ReviewCommitSelectionState(items=items, delivery=delivery)
        self._review_commit_options[key] = state
        self._review_commit_subjects[key] = subjects
        self._touch_cache_timestamp("review_commit_options", key)
        self._touch_cache_timestamp("review_commit_subjects", key)
        keyboard = self._build_review_commit_keyboard(state)
        await self._send_message(
            message.chat_id,
            self._selection_prompt(REVIEW_COMMIT_PICKER_PROMPT, state),
            thread_id=message.thread_id,
            reply_to=message.message_id,
            reply_markup=keyboard,
        )

    async def _list_recent_commits(
        self, record: TelegramTopicRecord
    ) -> list[tuple[str, str]]:
        try:
            client = await self._client_for_workspace(record.workspace_path)
        except AppServerUnavailableError:
            return []
        if client is None:
            return []
        command = "git log -n 50 --pretty=format:%H%x1f%s%x1e"
        try:
            result = await client.request(
                "command/exec",
                {
                    "cwd": record.workspace_path,
                    "command": ["bash", "-lc", command],
                    "timeoutMs": 10000,
                },
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.review.commit_list.failed",
                exc=exc,
            )
            return []
        stdout, _stderr, exit_code = _extract_command_result(result)
        if exit_code not in (None, 0) and not stdout.strip():
            return []
        return _parse_review_commit_log(stdout)

    async def _handle_bang_shell(
        self, message: TelegramMessage, text: str, _runtime: Any
    ) -> None:
        if not self._config.shell.enabled:
            await self._send_message(
                message.chat_id,
                "Shell commands are disabled. Enable telegram_bot.shell.enabled.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        record = await self._require_bound_record(message)
        if not record:
            return
        command_text = text[1:].strip()
        if not command_text:
            await self._send_message(
                message.chat_id,
                "Prefix a command with ! to run it locally.\nExample: !ls",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        try:
            client = await self._client_for_workspace(record.workspace_path)
        except AppServerUnavailableError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.app_server.unavailable",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                "App server unavailable; try again or check logs.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if client is None:
            await self._send_message(
                message.chat_id,
                "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        placeholder_id = await self._send_placeholder(
            message.chat_id,
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
        _approval_policy, sandbox_policy = self._effective_policies(record)
        params: dict[str, Any] = {
            "cwd": record.workspace_path,
            "command": ["bash", "-lc", command_text],
            "timeoutMs": self._config.shell.timeout_ms,
        }
        if sandbox_policy:
            params["sandboxPolicy"] = _normalize_sandbox_policy(sandbox_policy)
        timeout_seconds = max(0.1, self._config.shell.timeout_ms / 1000.0)
        request_timeout = timeout_seconds + 1.0
        try:
            result = await client.request(
                "command/exec", params, timeout=request_timeout
            )
        except asyncio.TimeoutError:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.shell.timeout",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                command=command_text,
                timeout_seconds=timeout_seconds,
            )
            timeout_label = int(math.ceil(timeout_seconds))
            timeout_message = (
                f"Shell command timed out after {timeout_label}s: `{command_text}`.\n"
                "Interactive commands (top/htop/watch/tail -f) do not exit. "
                "Try a one-shot flag like `top -l 1` (macOS) or "
                "`top -b -n 1` (Linux)."
            )
            await self._deliver_turn_response(
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                reply_to=message.message_id,
                placeholder_id=placeholder_id,
                response=_with_conversation_id(
                    timeout_message,
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                ),
            )
            return
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.shell.failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            await self._deliver_turn_response(
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                reply_to=message.message_id,
                placeholder_id=placeholder_id,
                response=_with_conversation_id(
                    "Shell command failed; check logs for details.",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                ),
            )
            return
        stdout, stderr, exit_code = _extract_command_result(result)
        full_body = _format_shell_body(command_text, stdout, stderr, exit_code)
        max_output_chars = min(
            self._config.shell.max_output_chars,
            TELEGRAM_MAX_MESSAGE_LENGTH - SHELL_MESSAGE_BUFFER_CHARS,
        )
        filename = f"shell-output-{secrets.token_hex(4)}.txt"
        response_text, attachment = _prepare_shell_response(
            full_body,
            max_output_chars=max_output_chars,
            filename=filename,
        )
        await self._deliver_turn_response(
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            reply_to=message.message_id,
            placeholder_id=placeholder_id,
            response=response_text,
        )
        if attachment is not None:
            await self._send_document(
                message.chat_id,
                attachment,
                filename=filename,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )

    async def _handle_diff(
        self, message: TelegramMessage, _args: str, _runtime: Any
    ) -> None:
        record = await self._require_bound_record(message)
        if not record:
            return
        client = await self._client_for_workspace(record.workspace_path)
        if client is None:
            await self._send_message(
                message.chat_id,
                "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        command = (
            "git rev-parse --is-inside-work-tree >/dev/null 2>&1 || "
            "{ echo 'Not a git repo'; exit 0; }\n"
            "git diff --color;\n"
            "git ls-files --others --exclude-standard | "
            'while read -r f; do git diff --color --no-index -- /dev/null "$f"; done'
        )
        try:
            result = await client.request(
                "command/exec",
                {
                    "cwd": record.workspace_path,
                    "command": ["bash", "-lc", command],
                    "timeoutMs": 10000,
                },
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.diff.failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                _with_conversation_id(
                    "Failed to compute diff; check logs for details.",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        output = _render_command_output(result)
        if not output.strip():
            output = "(No diff output.)"
        await self._send_message(
            message.chat_id,
            output,
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_mention(
        self, message: TelegramMessage, args: str, runtime: Any
    ) -> None:
        record = await self._require_bound_record(message)
        if not record:
            return
        argv = self._parse_command_args(args)
        if not argv:
            await self._send_message(
                message.chat_id,
                "Usage: /mention <path> [request]",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        workspace = canonicalize_path(Path(record.workspace_path or ""))
        path = Path(argv[0]).expanduser()
        if not path.is_absolute():
            path = workspace / path
        try:
            path = canonicalize_path(path)
        except Exception:
            await self._send_message(
                message.chat_id,
                "Could not resolve that path.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if not _path_within(workspace, path):
            await self._send_message(
                message.chat_id,
                "File must be within the bound workspace.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if not path.exists() or not path.is_file():
            await self._send_message(
                message.chat_id,
                "File not found.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        try:
            data = path.read_bytes()
        except Exception:
            await self._send_message(
                message.chat_id,
                "Failed to read file.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if len(data) > MAX_MENTION_BYTES:
            await self._send_message(
                message.chat_id,
                f"File too large (max {MAX_MENTION_BYTES} bytes).",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if _looks_binary(data):
            await self._send_message(
                message.chat_id,
                "File appears to be binary; refusing to include it.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        text = data.decode("utf-8", errors="replace")
        try:
            display_path = str(path.relative_to(workspace))
        except ValueError:
            display_path = str(path)
        request = " ".join(argv[1:]).strip()
        if not request:
            request = "Please review this file."
        prompt = "\n".join(
            [
                "Please use the file below as authoritative context.",
                "",
                f'<file path="{display_path}">',
                text,
                "</file>",
                "",
                f"My request: {request}",
            ]
        )
        await self._handle_normal_message(
            message,
            runtime,
            text_override=prompt,
            record=record,
        )

    async def _handle_skills(
        self, message: TelegramMessage, _args: str, _runtime: Any
    ) -> None:
        record = await self._require_bound_record(message)
        if not record:
            return
        client = await self._client_for_workspace(record.workspace_path)
        if client is None:
            await self._send_message(
                message.chat_id,
                "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        try:
            result = await client.request(
                "skills/list",
                {"cwds": [record.workspace_path], "forceReload": False},
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.skills.failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                _with_conversation_id(
                    "Failed to list skills; check logs for details.",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        await self._send_message(
            message.chat_id,
            _format_skills_list(result, record.workspace_path),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_mcp(
        self, message: TelegramMessage, _args: str, _runtime: Any
    ) -> None:
        record = await self._require_bound_record(message)
        if not record:
            return
        client = await self._client_for_workspace(record.workspace_path)
        if client is None:
            await self._send_message(
                message.chat_id,
                "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        try:
            result = await client.request(
                "mcpServerStatus/list",
                {"cursor": None, "limit": DEFAULT_MCP_LIST_LIMIT},
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.mcp.failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                _with_conversation_id(
                    "Failed to list MCP servers; check logs for details.",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        await self._send_message(
            message.chat_id,
            _format_mcp_list(result),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_experimental(
        self, message: TelegramMessage, args: str, _runtime: Any
    ) -> None:
        record = await self._require_bound_record(message)
        if not record:
            return
        client = await self._client_for_workspace(record.workspace_path)
        if client is None:
            await self._send_message(
                message.chat_id,
                "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        argv = self._parse_command_args(args)
        if not argv:
            try:
                result = await client.request(
                    "config/read",
                    {"includeLayers": False},
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.experimental.read_failed",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    exc=exc,
                )
                await self._send_message(
                    message.chat_id,
                    _with_conversation_id(
                        "Failed to read config; check logs for details.",
                        chat_id=message.chat_id,
                        thread_id=message.thread_id,
                    ),
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            await self._send_message(
                message.chat_id,
                _format_feature_flags(result),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if len(argv) < 2:
            await self._send_message(
                message.chat_id,
                "Usage: /experimental enable|disable <feature>",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        action = argv[0].lower()
        feature = argv[1].strip()
        if not feature:
            await self._send_message(
                message.chat_id,
                "Usage: /experimental enable|disable <feature>",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if action in ("enable", "on", "true", "1"):
            value = True
        elif action in ("disable", "off", "false", "0"):
            value = False
        else:
            await self._send_message(
                message.chat_id,
                "Usage: /experimental enable|disable <feature>",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        key_path = feature if feature.startswith("features.") else f"features.{feature}"
        try:
            await client.request(
                "config/value/write",
                {
                    "keyPath": key_path,
                    "value": value,
                    "mergeStrategy": "replace",
                },
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.experimental.write_failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                _with_conversation_id(
                    "Failed to update feature flag; check logs for details.",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        await self._send_message(
            message.chat_id,
            f"Feature {key_path} set to {value}.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_init(
        self, message: TelegramMessage, _args: str, runtime: Any
    ) -> None:
        record = await self._require_bound_record(message)
        if not record:
            return
        await self._handle_normal_message(
            message,
            runtime,
            text_override=INIT_PROMPT,
            record=record,
        )

    def _prepare_compact_summary_delivery(
        self, summary_text: str
    ) -> tuple[str, bytes | None]:
        summary_text = summary_text.strip() or "(no summary)"
        if len(summary_text) <= TELEGRAM_MAX_MESSAGE_LENGTH:
            return summary_text, None
        header = "Summary preview:\n"
        footer = "\n\nFull summary attached as compact-summary.txt"
        preview_limit = TELEGRAM_MAX_MESSAGE_LENGTH - len(header) - len(footer)
        if preview_limit < 20:
            preview_limit = 20
        preview = _compact_preview(summary_text, limit=preview_limit)
        display_text = f"{header}{preview}{footer}"
        if len(display_text) > TELEGRAM_MAX_MESSAGE_LENGTH:
            display_text = display_text[: TELEGRAM_MAX_MESSAGE_LENGTH - 3] + "..."
        return display_text, summary_text.encode("utf-8")

    async def _send_compact_summary_message(
        self,
        message: TelegramMessage,
        summary_text: str,
        *,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> tuple[Optional[int], str]:
        display_text, attachment = self._prepare_compact_summary_delivery(summary_text)
        payload_text, parse_mode = self._prepare_outgoing_text(
            display_text,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
        message_id = None
        try:
            response = await self._bot.send_message(
                message.chat_id,
                payload_text,
                message_thread_id=message.thread_id,
                reply_to_message_id=message.message_id,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
            )
            message_id = (
                response.get("message_id") if isinstance(response, dict) else None
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.compact.send_failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
        if attachment is not None:
            await self._send_document(
                message.chat_id,
                attachment,
                filename="compact-summary.txt",
                thread_id=message.thread_id,
                reply_to=message.message_id,
                caption="Full summary attached.",
            )
        return message_id if isinstance(message_id, int) else None, display_text

    def _build_compact_seed_prompt(self, summary_text: str) -> str:
        summary_text = summary_text.strip() or "(no summary)"
        return (
            "Context handoff from previous thread:\n\n"
            f"{summary_text}\n\n"
            "Continue from this context. Ask for missing info if needed."
        )

    async def _apply_compact_summary(
        self,
        message: TelegramMessage,
        record: "TelegramTopicRecord",
        summary_text: str,
    ) -> tuple[bool, str | None]:
        if not record.workspace_path:
            return False, "Topic not bound. Use /bind <repo_id> or /bind <path>."
        try:
            client = await self._client_for_workspace(record.workspace_path)
        except AppServerUnavailableError as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.app_server.unavailable",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            return False, "App server unavailable; try again or check logs."
        if client is None:
            return False, "Topic not bound. Use /bind <repo_id> or /bind <path>."
        log_event(
            self._logger,
            logging.INFO,
            "telegram.compact.apply.start",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            summary_len=len(summary_text),
            workspace_path=record.workspace_path,
        )
        try:
            agent = self._effective_agent(record)
            thread = await client.thread_start(record.workspace_path, agent=agent)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.compact.thread_start.failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            return False, "Failed to start a new thread."
        if not await self._require_thread_workspace(
            message, record.workspace_path, thread, action="thread_start"
        ):
            return False, "Failed to start a new thread."
        new_thread_id = _extract_thread_id(thread)
        if not new_thread_id:
            return False, "Failed to start a new thread."
        log_event(
            self._logger,
            logging.INFO,
            "telegram.compact.apply.thread_started",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            codex_thread_id=new_thread_id,
        )
        record = await self._apply_thread_result(
            message.chat_id,
            message.thread_id,
            thread,
            active_thread_id=new_thread_id,
        )
        seed_text = self._build_compact_seed_prompt(summary_text)
        record = await self._router.update_topic(
            message.chat_id,
            message.thread_id,
            lambda record: _set_pending_compact_seed(record, seed_text, new_thread_id),
        )
        log_event(
            self._logger,
            logging.INFO,
            "telegram.compact.apply.seed_queued",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            codex_thread_id=new_thread_id,
        )
        return True, None

    async def _handle_compact(
        self, message: TelegramMessage, args: str, runtime: Any
    ) -> None:
        argv = self._parse_command_args(args)
        if argv and argv[0].lower() in ("soft", "summary", "summarize"):
            record = await self._require_bound_record(message)
            if not record:
                return
            await self._handle_normal_message(
                message,
                runtime,
                text_override=COMPACT_SUMMARY_PROMPT,
                record=record,
            )
            return
        auto_apply = bool(argv and argv[0].lower() == "apply")
        record = await self._require_bound_record(message)
        if not record:
            return
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        if not record.active_thread_id:
            await self._send_message(
                message.chat_id,
                "No active thread to compact. Use /new to start one.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        conflict_key = await self._find_thread_conflict(
            record.active_thread_id, key=key
        )
        if conflict_key:
            await self._router.set_active_thread(
                message.chat_id, message.thread_id, None
            )
            await self._handle_thread_conflict(
                message,
                record.active_thread_id,
                conflict_key,
            )
            return
        verified = await self._verify_active_thread(message, record)
        if not verified:
            return
        record = verified
        outcome = await self._run_turn_and_collect_result(
            message,
            runtime,
            text_override=COMPACT_SUMMARY_PROMPT,
            record=record,
            allow_new_thread=False,
            missing_thread_message="No active thread to compact. Use /new to start one.",
            send_failure_response=True,
        )
        if isinstance(outcome, _TurnRunFailure):
            return
        summary_text = outcome.response.strip() or "(no summary)"
        reply_markup = None if auto_apply else build_compact_keyboard()
        summary_message_id, display_text = await self._send_compact_summary_message(
            message,
            summary_text,
            reply_markup=reply_markup,
        )
        if outcome.turn_id:
            self._token_usage_by_turn.pop(outcome.turn_id, None)
        await self._delete_message(message.chat_id, outcome.placeholder_id)
        await self._finalize_voice_transcript(
            message.chat_id,
            outcome.transcript_message_id,
            outcome.transcript_text,
        )
        await self._flush_outbox_files(
            record,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
        if auto_apply:
            success, failure_message = await self._apply_compact_summary(
                message, record, summary_text
            )
            if not success:
                await self._send_message(
                    message.chat_id,
                    failure_message or "Failed to start new thread with summary.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            await self._send_message(
                message.chat_id,
                "Started a new thread with the summary.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if summary_message_id is None:
            await self._send_message(
                message.chat_id,
                "Failed to send compact summary; try again.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        self._compact_pending[key] = CompactState(
            summary_text=summary_text,
            display_text=display_text,
            message_id=summary_message_id,
            created_at=now_iso(),
        )
        self._touch_cache_timestamp("compact_pending", key)

    async def _handle_compact_callback(
        self,
        key: str,
        callback: TelegramCallbackQuery,
        parsed: CompactCallback,
    ) -> None:
        async def _send_compact_status(text: str) -> bool:
            try:
                await self._send_message(
                    callback.chat_id,
                    text,
                    thread_id=callback.thread_id,
                    reply_to=callback.message_id,
                )
                return True
            except Exception:
                await self._send_message(
                    callback.chat_id,
                    text,
                    thread_id=callback.thread_id,
                )
                return True
            return False

        state = self._compact_pending.get(key)
        if not state or callback.message_id != state.message_id:
            await self._answer_callback(callback, "Selection expired")
            return
        if parsed.action == "cancel":
            log_event(
                self._logger,
                logging.INFO,
                "telegram.compact.callback.cancel",
                chat_id=callback.chat_id,
                thread_id=callback.thread_id,
                message_id=callback.message_id,
            )
            self._compact_pending.pop(key, None)
            if callback.chat_id is not None:
                await self._edit_message_text(
                    callback.chat_id,
                    state.message_id,
                    f"{state.display_text}\n\nCompact canceled.",
                    reply_markup=None,
                )
            await self._answer_callback(callback, "Canceled")
            return
        if parsed.action != "apply":
            await self._answer_callback(callback, "Selection expired")
            return
        log_event(
            self._logger,
            logging.INFO,
            "telegram.compact.callback.apply",
            chat_id=callback.chat_id,
            thread_id=callback.thread_id,
            message_id=callback.message_id,
            summary_len=len(state.summary_text),
        )
        self._compact_pending.pop(key, None)
        record = await self._router.get_topic(key)
        if record is None or not record.workspace_path:
            await self._answer_callback(callback, "Selection expired")
            return
        if callback.chat_id is None:
            return
        await self._answer_callback(callback, "Applying summary...")
        edited = await self._edit_message_text(
            callback.chat_id,
            state.message_id,
            f"{state.display_text}\n\nApplying summary...",
            reply_markup=None,
        )
        status = self._write_compact_status(
            "running",
            "Applying summary...",
            chat_id=callback.chat_id,
            thread_id=callback.thread_id,
            message_id=state.message_id,
            display_text=state.display_text,
        )
        if not edited:
            await _send_compact_status("Applying summary...")
        message = TelegramMessage(
            update_id=callback.update_id,
            message_id=callback.message_id or 0,
            chat_id=callback.chat_id,
            thread_id=callback.thread_id,
            from_user_id=callback.from_user_id,
            text=None,
            date=None,
            is_topic_message=callback.thread_id is not None,
        )
        success, failure_message = await self._apply_compact_summary(
            message,
            record,
            state.summary_text,
        )
        if not success:
            status = self._write_compact_status(
                "error",
                failure_message or "Failed to start new thread with summary.",
                chat_id=callback.chat_id,
                thread_id=callback.thread_id,
                message_id=state.message_id,
                display_text=state.display_text,
                error_detail=failure_message,
            )
            edited = await self._edit_message_text(
                callback.chat_id,
                state.message_id,
                f"{state.display_text}\n\nFailed to start new thread with summary.",
                reply_markup=None,
            )
            if edited:
                self._mark_compact_notified(status)
            elif await _send_compact_status("Failed to start new thread with summary."):
                self._mark_compact_notified(status)
            if failure_message:
                await self._send_message(
                    callback.chat_id,
                    failure_message,
                    thread_id=callback.thread_id,
                )
            return
        status = self._write_compact_status(
            "ok",
            "Summary applied.",
            chat_id=callback.chat_id,
            thread_id=callback.thread_id,
            message_id=state.message_id,
            display_text=state.display_text,
        )
        edited = await self._edit_message_text(
            callback.chat_id,
            state.message_id,
            f"{state.display_text}\n\nSummary applied.",
            reply_markup=None,
        )
        if edited:
            self._mark_compact_notified(status)
        elif await _send_compact_status("Summary applied."):
            self._mark_compact_notified(status)

    async def _handle_rollout(
        self, message: TelegramMessage, _args: str, _runtime: Any
    ) -> None:
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        record = await self._router.get_topic(key)
        if record is None or not record.active_thread_id or not record.workspace_path:
            await self._send_message(
                message.chat_id,
                "No active thread to inspect.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        client = await self._client_for_workspace(record.workspace_path)
        if client is None:
            await self._send_message(
                message.chat_id,
                "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if record.rollout_path:
            await self._send_message(
                message.chat_id,
                f"Rollout path: {record.rollout_path}",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        rollout_path = None
        try:
            result = await client.thread_resume(record.active_thread_id)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.rollout.failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                _with_conversation_id(
                    "Failed to look up rollout path; check logs for details.",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        rollout_path = _extract_thread_info(result).get("rollout_path")
        if not rollout_path:
            try:
                threads, _ = await self._list_threads_paginated(
                    client,
                    limit=THREAD_LIST_PAGE_LIMIT,
                    max_pages=THREAD_LIST_MAX_PAGES,
                    needed_ids={record.active_thread_id},
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.rollout.failed",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    exc=exc,
                )
                await self._send_message(
                    message.chat_id,
                    _with_conversation_id(
                        "Failed to look up rollout path; check logs for details.",
                        chat_id=message.chat_id,
                        thread_id=message.thread_id,
                    ),
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            entry = _find_thread_entry(threads, record.active_thread_id)
            rollout_path = _extract_rollout_path(entry) if entry else None
        if rollout_path:
            await self._router.update_topic(
                message.chat_id,
                message.thread_id,
                lambda record: _set_rollout_path(record, rollout_path),
            )
            await self._send_message(
                message.chat_id,
                f"Rollout path: {rollout_path}",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        await self._send_message(
            message.chat_id,
            "Rollout path not available.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
        await self._send_message(
            message.chat_id,
            "Rollout path not found for this thread.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _start_update(
        self,
        *,
        chat_id: int,
        thread_id: Optional[int],
        update_target: str,
        reply_to: Optional[int] = None,
        callback: Optional[TelegramCallbackQuery] = None,
        selection_key: Optional[str] = None,
    ) -> None:
        repo_url = (self._update_repo_url or DEFAULT_UPDATE_REPO_URL).strip()
        if not repo_url:
            repo_url = DEFAULT_UPDATE_REPO_URL
        repo_ref = (self._update_repo_ref or DEFAULT_UPDATE_REPO_REF).strip()
        if not repo_ref:
            repo_ref = DEFAULT_UPDATE_REPO_REF
        update_dir = Path.home() / ".codex-autorunner" / "update_cache"
        notify_reply_to = reply_to
        if notify_reply_to is None and callback is not None:
            notify_reply_to = callback.message_id
        try:
            _spawn_update_process(
                repo_url=repo_url,
                repo_ref=repo_ref,
                update_dir=update_dir,
                logger=self._logger,
                update_target=update_target,
                notify_chat_id=chat_id,
                notify_thread_id=thread_id,
                notify_reply_to=notify_reply_to,
            )
            log_event(
                self._logger,
                logging.INFO,
                "telegram.update.started",
                chat_id=chat_id,
                thread_id=thread_id,
                repo_ref=repo_ref,
                update_target=update_target,
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.update.failed",
                chat_id=chat_id,
                thread_id=thread_id,
                repo_ref=repo_ref,
                update_target=update_target,
                exc=exc,
            )
            failure = _with_conversation_id(
                "Update failed to start; check logs for details.",
                chat_id=chat_id,
                thread_id=thread_id,
            )
            if callback and selection_key:
                await self._answer_callback(callback, "Update failed")
                await self._finalize_selection(selection_key, callback, failure)
            else:
                await self._send_message(
                    chat_id,
                    failure,
                    thread_id=thread_id,
                    reply_to=reply_to,
                )
            return
        message = (
            f"Update started ({update_target}). The selected service(s) will restart."
        )
        if callback and selection_key:
            await self._answer_callback(callback, "Update started")
            await self._finalize_selection(selection_key, callback, message)
        else:
            await self._send_message(
                chat_id,
                message,
                thread_id=thread_id,
                reply_to=reply_to,
            )
        self._schedule_update_status_watch(chat_id, thread_id)

    async def _prompt_update_selection(
        self,
        message: TelegramMessage,
        *,
        prompt: str = UPDATE_PICKER_PROMPT,
    ) -> None:
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        state = SelectionState(items=list(UPDATE_TARGET_OPTIONS))
        keyboard = self._build_update_keyboard(state)
        self._update_options[key] = state
        self._touch_cache_timestamp("update_options", key)
        await self._send_message(
            message.chat_id,
            prompt,
            thread_id=message.thread_id,
            reply_to=message.message_id,
            reply_markup=keyboard,
        )

    async def _prompt_update_selection_from_callback(
        self,
        key: str,
        callback: TelegramCallbackQuery,
        *,
        prompt: str = UPDATE_PICKER_PROMPT,
    ) -> None:
        state = SelectionState(items=list(UPDATE_TARGET_OPTIONS))
        keyboard = self._build_update_keyboard(state)
        self._update_options[key] = state
        self._touch_cache_timestamp("update_options", key)
        await self._update_selection_message(key, callback, prompt, keyboard)

    def _has_active_turns(self) -> bool:
        return bool(self._turn_contexts)

    async def _prompt_update_confirmation(self, message: TelegramMessage) -> None:
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        self._update_confirm_options[key] = True
        self._touch_cache_timestamp("update_confirm_options", key)
        await self._send_message(
            message.chat_id,
            "An active Codex turn is running. Updating will restart the service. Continue?",
            thread_id=message.thread_id,
            reply_to=message.message_id,
            reply_markup=build_update_confirm_keyboard(),
        )

    def _update_status_path(self) -> Path:
        return Path.home() / ".codex-autorunner" / "update_status.json"

    def _read_update_status(self) -> Optional[dict[str, Any]]:
        path = self._update_status_path()
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def _format_update_status_message(self, status: Optional[dict[str, Any]]) -> str:
        if not status:
            return "No update status recorded."
        state = str(status.get("status") or "unknown")
        message = str(status.get("message") or "")
        timestamp = status.get("at")
        rendered_time = ""
        if isinstance(timestamp, (int, float)):
            rendered_time = datetime.fromtimestamp(timestamp).isoformat(
                timespec="seconds"
            )
        lines = [f"Update status: {state}"]
        if message:
            lines.append(f"Message: {message}")
        if rendered_time:
            lines.append(f"Last updated: {rendered_time}")
        return "\n".join(lines)

    async def _handle_update_status(
        self, message: TelegramMessage, reply_to: Optional[int] = None
    ) -> None:
        status = self._read_update_status()
        await self._send_message(
            message.chat_id,
            self._format_update_status_message(status),
            thread_id=message.thread_id,
            reply_to=reply_to or message.message_id,
        )

    def _schedule_update_status_watch(
        self,
        chat_id: int,
        thread_id: Optional[int],
        *,
        timeout_seconds: float = 300.0,
        interval_seconds: float = 2.0,
    ) -> None:
        async def _watch() -> None:
            deadline = time.monotonic() + timeout_seconds
            while time.monotonic() < deadline:
                status = self._read_update_status()
                if status and status.get("status") in ("ok", "error", "rollback"):
                    await self._send_message(
                        chat_id,
                        self._format_update_status_message(status),
                        thread_id=thread_id,
                    )
                    return
                await asyncio.sleep(interval_seconds)
            await self._send_message(
                chat_id,
                "Update still running. Use /update status for the latest state.",
                thread_id=thread_id,
            )

        self._spawn_task(_watch())

    def _mark_update_notified(self, status: dict[str, Any]) -> None:
        path = self._update_status_path()
        updated = dict(status)
        updated["notify_sent_at"] = time.time()
        try:
            path.write_text(json.dumps(updated), encoding="utf-8")
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.update.notify_write_failed",
                exc=exc,
            )

    def _compact_status_path(self) -> Path:
        return Path.home() / ".codex-autorunner" / "compact_status.json"

    def _read_compact_status(self) -> Optional[dict[str, Any]]:
        path = self._compact_status_path()
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    def _write_compact_status(
        self, status: str, message: str, **extra: Any
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "status": status,
            "message": message,
            "at": time.time(),
        }
        payload.update(extra)
        path = self._compact_status_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.compact.status_write_failed",
                exc=exc,
            )
        return payload

    def _mark_compact_notified(self, status: dict[str, Any]) -> None:
        path = self._compact_status_path()
        updated = dict(status)
        updated["notify_sent_at"] = time.time()
        try:
            path.write_text(json.dumps(updated), encoding="utf-8")
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.compact.notify_write_failed",
                exc=exc,
            )

    async def _maybe_send_update_status_notice(self) -> None:
        status = self._read_update_status()
        if not status:
            return
        notify_chat_id = status.get("notify_chat_id")
        if not isinstance(notify_chat_id, int):
            return
        if status.get("notify_sent_at"):
            return
        notify_thread_id = status.get("notify_thread_id")
        if not isinstance(notify_thread_id, int):
            notify_thread_id = None
        notify_reply_to = status.get("notify_reply_to")
        if not isinstance(notify_reply_to, int):
            notify_reply_to = None
        state = str(status.get("status") or "")
        if state in ("running", "spawned"):
            self._schedule_update_status_watch(notify_chat_id, notify_thread_id)
            return
        if state not in ("ok", "error", "rollback"):
            return
        await self._send_message(
            notify_chat_id,
            self._format_update_status_message(status),
            thread_id=notify_thread_id,
            reply_to=notify_reply_to,
        )
        self._mark_update_notified(status)

    async def _maybe_send_compact_status_notice(self) -> None:
        status = self._read_compact_status()
        if not status or status.get("notify_sent_at"):
            return
        chat_id = status.get("chat_id")
        if not isinstance(chat_id, int):
            return
        thread_id = status.get("thread_id")
        if not isinstance(thread_id, int):
            thread_id = None
        message_id = status.get("message_id")
        if not isinstance(message_id, int):
            message_id = None
        display_text = status.get("display_text")
        if not isinstance(display_text, str):
            display_text = None
        state = str(status.get("status") or "")
        message = str(status.get("message") or "")
        if state == "running":
            message = "Compact apply interrupted by restart. Please retry."
            status = self._write_compact_status(
                "interrupted",
                message,
                chat_id=chat_id,
                thread_id=thread_id,
                message_id=message_id,
                display_text=display_text,
                started_at=status.get("at"),
            )
        sent = False
        if message_id is not None and display_text is not None and message:
            edited = await self._edit_message_text(
                chat_id,
                message_id,
                f"{display_text}\n\n{message}",
                reply_markup=None,
            )
            sent = edited
        if not sent and message:
            try:
                await self._send_message(
                    chat_id,
                    message,
                    thread_id=thread_id,
                    reply_to=message_id,
                )
                sent = True
            except Exception:
                try:
                    await self._send_message(chat_id, message, thread_id=thread_id)
                    sent = True
                except Exception:
                    sent = False
        if sent:
            self._mark_compact_notified(status)

    async def _handle_update(
        self, message: TelegramMessage, args: str, _runtime: Any
    ) -> None:
        argv = self._parse_command_args(args)
        target_raw = argv[0] if argv else None
        if target_raw and target_raw.lower() == "status":
            await self._handle_update_status(message)
            return
        if not target_raw:
            if self._has_active_turns():
                await self._prompt_update_confirmation(message)
            else:
                await self._prompt_update_selection(message)
            return
        try:
            update_target = _normalize_update_target(target_raw)
        except ValueError:
            await self._prompt_update_selection(
                message,
                prompt="Unknown update target. Select update target (buttons below).",
            )
            return
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        self._update_options.pop(key, None)
        await self._start_update(
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            update_target=update_target,
            reply_to=message.message_id,
        )

    async def _handle_logout(
        self, message: TelegramMessage, _args: str, _runtime: Any
    ) -> None:
        record = await self._require_bound_record(message)
        if not record:
            return
        client = await self._client_for_workspace(record.workspace_path)
        if client is None:
            await self._send_message(
                message.chat_id,
                "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        try:
            await client.request("account/logout", params=None)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.logout.failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                _with_conversation_id(
                    "Logout failed; check logs for details.",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        await self._send_message(
            message.chat_id,
            "Logged out.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_feedback(
        self, message: TelegramMessage, args: str, _runtime: Any
    ) -> None:
        reason = args.strip()
        if not reason:
            await self._send_message(
                message.chat_id,
                "Usage: /feedback <reason>",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        record = await self._require_bound_record(message)
        if not record:
            return
        client = await self._client_for_workspace(record.workspace_path)
        if client is None:
            await self._send_message(
                message.chat_id,
                "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        params: dict[str, Any] = {
            "classification": "bug",
            "reason": reason,
            "includeLogs": True,
        }
        if record and record.active_thread_id:
            params["threadId"] = record.active_thread_id
        try:
            result = await client.request("feedback/upload", params)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.feedback.failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                _with_conversation_id(
                    "Feedback upload failed; check logs for details.",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        report_id = None
        if isinstance(result, dict):
            report_id = result.get("threadId") or result.get("id")
        message_text = "Feedback sent."
        if isinstance(report_id, str):
            message_text = f"Feedback sent (report {report_id})."
        await self._send_message(
            message.chat_id,
            message_text,
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
