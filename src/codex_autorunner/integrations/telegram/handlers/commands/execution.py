from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import secrets
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import httpx

from .....agents.opencode.client import OpenCodeProtocolError
from .....agents.opencode.runtime import (
    PERMISSION_ALLOW,
    PERMISSION_ASK,
    build_turn_id,
    collect_opencode_output,
    extract_session_id,
    format_permission_prompt,
    map_approval_policy_to_permission,
    opencode_missing_env,
    split_model_id,
)
from .....agents.opencode.supervisor import OpenCodeSupervisorError
from .....core.config import load_repo_config
from .....core.injected_context import wrap_injected_context
from .....core.logging_utils import log_event
from .....core.state import now_iso
from .....core.utils import canonicalize_path
from .....integrations.github.service import GitHubService
from ....app_server.client import (
    CodexAppServerClient,
    CodexAppServerDisconnected,
    _normalize_sandbox_policy,
)
from ...adapter import (
    TelegramMessage,
)
from ...config import AppServerUnavailableError
from ...constants import (
    DEFAULT_INTERRUPT_TIMEOUT_SECONDS,
    MAX_MENTION_BYTES,
    MAX_TOPIC_THREAD_HISTORY,
    OPENCODE_TURN_TIMEOUT_SECONDS,
    PLACEHOLDER_TEXT,
    QUEUED_PLACEHOLDER_TEXT,
    RESUME_PREVIEW_ASSISTANT_LIMIT,
    RESUME_PREVIEW_USER_LIMIT,
    SHELL_MESSAGE_BUFFER_CHARS,
    TELEGRAM_MAX_MESSAGE_LENGTH,
    WHISPER_TRANSCRIPT_DISCLAIMER,
    TurnKey,
)
from ...helpers import (
    _clear_pending_compact_seed,
    _compact_preview,
    _compose_agent_response,
    _compose_interrupt_response,
    _extract_command_result,
    _extract_thread_id,
    _format_shell_body,
    _looks_binary,
    _path_within,
    _prepare_shell_response,
    _preview_from_text,
    _render_command_output,
    _repo_root,
    _set_thread_summary,
    _with_conversation_id,
    find_github_links,
    is_interrupt_status,
)

if TYPE_CHECKING:
    from ...state import TelegramTopicRecord

from .shared import SharedHelpers

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

_GENERIC_TELEGRAM_ERRORS = {
    "Telegram request failed",
    "Telegram file download failed",
    "Telegram API returned error",
}

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


def _coerce_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except Exception:
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


class ExecutionCommands(SharedHelpers):
    """Execution-related command handlers for Telegram integration."""

    def _maybe_append_whisper_disclaimer(
        self, prompt_text: str, *, transcript_text: Optional[str]
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

    async def _handle_bang_shell(
        self, message: TelegramMessage, text: str, _runtime: Any
    ) -> None:
        """Handle !shell command."""
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
        """Handle /diff command."""
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
        """Handle @mention command."""
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

    async def _execute_opencode_turn(
        self,
        message: TelegramMessage,
        runtime: Any,
        record: "TelegramTopicRecord",
        prompt_text: str,
        thread_id: Optional[str],
        key: str,
        turn_semaphore: asyncio.Semaphore,
        *,
        placeholder_id: Optional[int],
        placeholder_text: str,
        send_failure_response: bool,
        allow_new_thread: bool,
        missing_thread_message: Optional[str],
        transcript_message_id: Optional[int],
        transcript_text: Optional[str],
    ) -> _TurnRunResult | _TurnRunFailure:
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
                        record.thread_ids = record.thread_ids[:MAX_TOPIC_THREAD_HISTORY]
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

            user_preview = _preview_from_text(prompt_text, RESUME_PREVIEW_USER_LIMIT)
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
                queued=turn_semaphore.locked(),
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
            turn_started_at: Optional[float] = None
            turn_id = None
            turn_elapsed_seconds = None

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
                    queued=turn_semaphore.locked(),
                    max_parallel_turns=self._config.concurrency.max_parallel_turns,
                    per_topic_queue=self._config.concurrency.per_topic_queue,
                )
                if (
                    turn_semaphore.locked()
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
                    from ...types import TurnContext

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

                    approval_policy, _sandbox_policy = self._effective_policies(record)
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
                            props.get("questions") if isinstance(props, dict) else None
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
                                        thread_id=thread_id,
                                    )
                                    await self._note_progress_context_usage(
                                        token_usage,
                                        turn_id=turn_id,
                                        thread_id=thread_id,
                                    )
                        await self._schedule_progress_edit(turn_key)

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
                        )
                    )
                    prompt_task = asyncio.create_task(
                        opencode_client.prompt(
                            thread_id,
                            message=prompt_text,
                            model=model_payload,
                        )
                    )
                    try:
                        await prompt_task
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

            token_usage = self._token_usage_by_turn.get(turn_id) if turn_id else None
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
            if runtime.current_turn_key == (thread_id, turn_id):
                runtime.current_turn_id = None
                runtime.current_turn_key = None
            runtime.interrupt_requested = False

    async def _execute_codex_turn(
        self,
        message: TelegramMessage,
        runtime: Any,
        record: "TelegramTopicRecord",
        prompt_text: str,
        thread_id: Optional[str],
        key: str,
        turn_semaphore: asyncio.Semaphore,
        input_items: Optional[list[dict[str, Any]]],
        *,
        placeholder_id: Optional[int],
        placeholder_text: str,
        send_failure_response: bool,
        allow_new_thread: bool,
        missing_thread_message: Optional[str],
        transcript_message_id: Optional[int],
        transcript_text: Optional[str],
    ) -> _TurnRunResult | _TurnRunFailure:
        turn_handle = None
        turn_key: Optional[TurnKey] = None
        turn_started_at: Optional[float] = None

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
                queued=turn_semaphore.locked(),
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
            turn_started_at: Optional[float] = None
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
                    queued=turn_semaphore.locked(),
                    max_parallel_turns=self._config.concurrency.max_parallel_turns,
                    per_topic_queue=self._config.concurrency.per_topic_queue,
                )
                if (
                    turn_semaphore.locked()
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
                from ...types import TurnContext

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

    def _prepare_turn_prompt(
        self, prompt_text: str, *, transcript_text: Optional[str] = None
    ) -> str:
        prompt_text = self._maybe_append_whisper_disclaimer(
            prompt_text, transcript_text=transcript_text
        )
        return prompt_text

    async def _prepare_turn_context(
        self,
        message: TelegramMessage,
        prompt_text: str,
        record: "TelegramTopicRecord",
    ) -> tuple[str, str]:
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)

        prompt_text, injected = await self._maybe_inject_github_context(
            prompt_text, record
        )
        if injected:
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

        return prompt_text, key

    async def _prepare_turn_placeholder(
        self,
        message: TelegramMessage,
        *,
        placeholder_id: Optional[int],
        send_placeholder: bool,
        queued: bool,
    ) -> Optional[int]:
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
            key = await self._resolve_topic_key(message.chat_id, message.thread_id)
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
        return placeholder_id

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
        prompt_text = self._prepare_turn_prompt(
            prompt_text, transcript_text=transcript_text
        )
        prompt_text, key = await self._prepare_turn_context(
            message, prompt_text, record
        )

        turn_semaphore = self._ensure_turn_semaphore()
        queued = turn_semaphore.locked()
        placeholder_text = QUEUED_PLACEHOLDER_TEXT if queued else PLACEHOLDER_TEXT
        placeholder_id = await self._prepare_turn_placeholder(
            message,
            placeholder_id=placeholder_id,
            send_placeholder=send_placeholder,
            queued=queued,
        )

        agent = self._effective_agent(record)
        if agent == "opencode":
            return await self._execute_opencode_turn(
                message,
                runtime,
                record,
                prompt_text,
                thread_id,
                key,
                turn_semaphore,
                placeholder_id=placeholder_id,
                placeholder_text=placeholder_text,
                send_failure_response=send_failure_response,
                allow_new_thread=allow_new_thread,
                missing_thread_message=missing_thread_message,
                transcript_message_id=transcript_message_id,
                transcript_text=transcript_text,
            )

        return await self._execute_codex_turn(
            message,
            runtime,
            record,
            prompt_text,
            thread_id,
            key,
            turn_semaphore,
            input_items,
            placeholder_id=placeholder_id,
            placeholder_text=placeholder_text,
            send_failure_response=send_failure_response,
            allow_new_thread=allow_new_thread,
            missing_thread_message=missing_thread_message,
            transcript_message_id=transcript_message_id,
            transcript_text=transcript_text,
        )
