"""Shared helper methods for Telegram command handlers."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import secrets
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import httpx

from .....agents.opencode.client import OpenCodeProtocolError
from .....agents.opencode.supervisor import OpenCodeSupervisorError
from .....agents.registry import get_registered_agents
from .....agents.types import normalize_runtime_capabilities
from .....core.coercion import coerce_int
from .....core.logging_utils import log_event
from .....core.utils import canonicalize_path
from ....app_server.client import _normalize_sandbox_policy
from ....chat.agents import DEFAULT_CHAT_AGENT, normalize_chat_agent
from ....chat.constants import APP_SERVER_UNAVAILABLE_MESSAGE, TOPIC_NOT_BOUND_MESSAGE
from ...adapter import (
    InlineButton,
    TelegramMessage,
    build_inline_keyboard,
    encode_cancel_callback,
)
from ...config import AppServerUnavailableError
from ...constants import (
    MAX_MENTION_BYTES,
    SHELL_MESSAGE_BUFFER_CHARS,
    TELEGRAM_MAX_MESSAGE_LENGTH,
    TurnKey,
)
from ...helpers import (
    _extract_command_result,
    _format_shell_body,
    _looks_binary,
    _path_within,
    _prepare_shell_response,
    _render_command_output,
    _with_conversation_id,
    format_public_error,
)
from ...payload_utils import (
    extract_opencode_error_detail,
    extract_opencode_session_path,
)

if TYPE_CHECKING:
    pass


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
class _RuntimeStub:
    current_turn_id: Optional[str] = None
    current_turn_key: Optional[TurnKey] = None
    interrupt_requested: bool = False
    interrupt_message_id: Optional[int] = None
    interrupt_turn_id: Optional[str] = None


class TelegramCommandSupportMixin:
    """Shared helper methods for Telegram command handlers.

    This mixin provides common utilities for error formatting, argument parsing,
    and OpenCode session handling across Telegram command handler classes.
    All methods use `self` to access instance attributes.
    """

    def _coerce_optional_int(self, value: Any) -> Optional[int]:
        """Safely coerce value to int, rejecting bool.

        Args:
            value: Value to coerce to int.

        Returns:
            Integer value if coercion succeeds and value is not bool, None otherwise.
        """
        return coerce_int(value)

    def _agent_descriptor(self, agent: object) -> Any:
        normalized = normalize_chat_agent(
            agent,
            default=DEFAULT_CHAT_AGENT,
            context=self,
        )
        if normalized is None:
            return None
        try:
            descriptors = get_registered_agents(self)
        except TypeError as exc:
            if "positional argument" not in str(exc):
                raise
            descriptors = get_registered_agents()
        return descriptors.get(normalized)

    def _agent_display_name(self, agent: object) -> str:
        descriptor = self._agent_descriptor(agent)
        if descriptor is not None:
            return descriptor.name
        normalized = normalize_chat_agent(agent, context=self)
        if isinstance(normalized, str) and normalized:
            return normalized
        if isinstance(agent, str) and agent.strip():
            return agent.strip()
        return "This agent"

    def _agent_supports_capability(self, agent: object, capability: str) -> bool:
        descriptor = self._agent_descriptor(agent)
        if descriptor is None:
            return False
        normalized = normalize_runtime_capabilities([capability])
        if not normalized:
            return False
        return next(iter(normalized)) in descriptor.capabilities

    def _agents_supporting_capability(self, capability: str) -> list[str]:
        normalized = normalize_runtime_capabilities([capability])
        if not normalized:
            return []
        resolved = next(iter(normalized))
        try:
            descriptors = get_registered_agents(self)
        except TypeError as exc:
            if "positional argument" not in str(exc):
                raise
            descriptors = get_registered_agents()
        return sorted(
            descriptor.id
            for descriptor in descriptors.values()
            if resolved in descriptor.capabilities
        )

    def _format_httpx_exception(self, exc: Exception) -> Optional[str]:
        """Format httpx exceptions for user-friendly error messages.

        Args:
            exc: Exception to format.

        Returns:
            Formatted error message string or None if exception is not an httpx error.
        """
        if isinstance(exc, httpx.HTTPStatusError):
            try:
                payload = exc.response.json()
            except (json.JSONDecodeError, ValueError):
                payload = None
            if isinstance(payload, dict):
                detail = (
                    payload.get("detail")
                    or payload.get("message")
                    or payload.get("error")
                )
                if isinstance(detail, str) and detail:
                    return format_public_error(detail)
            response_text = exc.response.text.strip()
            if response_text:
                return format_public_error(response_text)
            return f"Request failed (HTTP {exc.response.status_code})."
        if isinstance(exc, httpx.RequestError):
            detail = str(exc).strip()
            if detail:
                return format_public_error(detail)
            return "Request failed."
        return None

    def _format_opencode_exception(self, exc: Exception) -> Optional[str]:
        """Format OpenCode exceptions for user-friendly error messages.

        Args:
            exc: Exception to format.

        Returns:
            Formatted error message string or None if exception is not recognized.
        """
        if isinstance(exc, OpenCodeSupervisorError):
            detail = str(exc).strip()
            if detail:
                return f"OpenCode backend unavailable ({format_public_error(detail)})."
            return "OpenCode backend unavailable."
        if isinstance(exc, OpenCodeProtocolError):
            detail = str(exc).strip()
            if detail:
                return f"OpenCode protocol error: {format_public_error(detail)}"
            return "OpenCode protocol error."
        if isinstance(exc, json.JSONDecodeError):
            return "OpenCode returned invalid JSON."
        if isinstance(exc, httpx.HTTPStatusError):
            detail = None
            try:
                detail = self._extract_opencode_error_detail(exc.response.json())
            except (json.JSONDecodeError, ValueError):
                detail = None
            if detail:
                return f"OpenCode error: {format_public_error(detail)}"
            response_text = exc.response.text.strip()
            if response_text:
                return f"OpenCode error: {format_public_error(response_text)}"
            return f"OpenCode request failed (HTTP {exc.response.status_code})."
        if isinstance(exc, httpx.RequestError):
            detail = str(exc).strip()
            if detail:
                return f"OpenCode request failed: {format_public_error(detail)}"
            return "OpenCode request failed."
        return None

    def _extract_opencode_error_detail(self, payload: Any) -> Optional[str]:
        """Extract error detail from OpenCode response payload.

        Args:
            payload: Response payload to extract error detail from.

        Returns:
            Error detail string if found, None otherwise.
        """
        return extract_opencode_error_detail(payload)

    def _extract_opencode_session_path(self, payload: Any) -> Optional[str]:
        """Extract session path from OpenCode payload.

        Args:
            payload: Payload to extract session path from.

        Returns:
            Session path string if found, None otherwise.
        """
        return extract_opencode_session_path(payload)

    def _is_missing_opencode_session_error(self, exc: Exception) -> bool:
        """Return true when an OpenCode error indicates a missing session."""

        markers = (
            "session not found",
            "no session found",
            "unknown session",
            "session does not exist",
            "session no longer exists",
            "missing session",
        )
        current: Optional[BaseException] = exc
        seen: set[int] = set()
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            if isinstance(current, httpx.HTTPStatusError):
                if current.response.status_code == 404:
                    return True
                detail = None
                try:
                    detail = self._extract_opencode_error_detail(
                        current.response.json()
                    )
                except (json.JSONDecodeError, ValueError):
                    detail = None
                candidates = [detail, current.response.text, str(current)]
            else:
                candidates = [str(current)]
            for candidate in candidates:
                if not isinstance(candidate, str):
                    continue
                lowered = candidate.lower()
                if any(marker in lowered for marker in markers):
                    return True
                if "session" in lowered and "not found" in lowered:
                    return True
            current = current.__cause__ or current.__context__
        return False

    def _opencode_session_stall_timeout_seconds(self) -> Optional[float]:
        """Return configured OpenCode stream stall timeout when available."""

        opencode_cfg = getattr(getattr(self, "_config", None), "opencode", None)
        value = getattr(opencode_cfg, "session_stall_timeout_seconds", None)
        if value is None:
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        if parsed <= 0:
            return None
        return parsed

    def _interrupt_keyboard(self) -> dict[str, Any]:
        """Build interrupt button keyboard.

        Returns:
            Inline keyboard with a cancel/interrupt button.
        """
        return build_inline_keyboard(
            [[InlineButton("Cancel", encode_cancel_callback("interrupt"))]]
        )

    async def _clear_interrupt_status_message(
        self,
        *,
        chat_id: int,
        runtime: Any,
        turn_id: Optional[str],
        fallback_text: Optional[str] = None,
        outcome_visible: bool = True,
    ) -> None:
        """Clear an interrupt status message once the turn outcome is already visible.

        Prefer deleting the transient "Stopping current turn..." message so Telegram
        does not restate the same terminal outcome in a second message. When the turn
        outcome was not delivered, preserve a terminal signal by editing in place.
        """

        if runtime.interrupt_turn_id != turn_id:
            return
        message_id = runtime.interrupt_message_id
        if message_id is None:
            runtime.interrupt_turn_id = None
            return
        cleared = False
        if outcome_visible:
            cleared = await self._delete_message(chat_id, message_id)
        if not cleared and fallback_text:
            await self._edit_message_text(chat_id, message_id, fallback_text)
        runtime.interrupt_message_id = None
        runtime.interrupt_turn_id = None

    def _parse_command_args(self, args: str) -> list[str]:
        """Parse command arguments with shlex.

        Args:
            args: Command argument string.

        Returns:
            List of parsed argument tokens.
        """
        if not args:
            return []
        try:
            return [part for part in shlex.split(args) if part]
        except ValueError:
            return [part for part in args.split() if part]

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
                APP_SERVER_UNAVAILABLE_MESSAGE,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if client is None:
            await self._send_message(
                message.chat_id,
                TOPIC_NOT_BOUND_MESSAGE,
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
        except (httpx.HTTPStatusError, httpx.RequestError, OSError) as exc:
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
                TOPIC_NOT_BOUND_MESSAGE,
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
        except (httpx.HTTPStatusError, httpx.RequestError, OSError) as exc:
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
        except (OSError, ValueError):
            await self._send_message(
                message.chat_id,
                "Could not resolve that path.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if not _path_within(root=workspace, target=path):
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
        except OSError:
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
