from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import httpx

from ....agents.opencode.client import OpenCodeProtocolError
from ....agents.opencode.supervisor import OpenCodeSupervisorError
from ....core.logging_utils import log_event
from ....core.state import now_iso
from ....core.update import _normalize_update_target, _spawn_update_process
from ..adapter import (
    CompactCallback,
    InlineButton,
    TelegramCallbackQuery,
    TelegramCommand,
    TelegramMessage,
    build_compact_keyboard,
    build_inline_keyboard,
    build_update_confirm_keyboard,
    encode_cancel_callback,
)
from ..config import AppServerUnavailableError
from ..constants import (
    COMMAND_DISABLED_TEMPLATE,
    COMPACT_SUMMARY_PROMPT,
    DEFAULT_MCP_LIST_LIMIT,
    DEFAULT_MODEL_LIST_LIMIT,
    DEFAULT_UPDATE_REPO_REF,
    DEFAULT_UPDATE_REPO_URL,
    INIT_PROMPT,
    MODEL_PICKER_PROMPT,
    THREAD_LIST_MAX_PAGES,
    THREAD_LIST_PAGE_LIMIT,
    UPDATE_PICKER_PROMPT,
    UPDATE_TARGET_OPTIONS,
    VALID_REASONING_EFFORTS,
    TurnKey,
)
from ..helpers import (
    _coerce_model_options,
    _compact_preview,
    _extract_rollout_path,
    _extract_thread_id,
    _extract_thread_info,
    _find_thread_entry,
    _format_feature_flags,
    _format_help_text,
    _format_mcp_list,
    _format_model_list,
    _format_skills_list,
    _set_model_overrides,
    _set_pending_compact_seed,
    _set_rollout_path,
    _thread_summary_preview,
    _with_conversation_id,
)
from ..state import (
    parse_topic_key,
    topic_key,
)
from ..types import (
    CompactState,
    ModelPickerState,
    SelectionState,
)

if TYPE_CHECKING:
    from ..state import TelegramTopicRecord

from .commands import (
    ApprovalsCommands,
    ExecutionCommands,
    FilesCommands,
    FormattingHelpers,
    GitHubCommands,
    VoiceCommands,
    WorkspaceCommands,
)
from .commands.execution import _TurnRunFailure

PROMPT_CONTEXT_RE = re.compile("\\bprompt\\b", re.IGNORECASE)
PROMPT_CONTEXT_HINT = (
    "If the user asks to write a prompt, put the prompt in a ```code block```."
)
OUTBOX_CONTEXT_RE = re.compile(
    "(?:\\b(?:pdf|png|jpg|jpeg|gif|webp|svg|csv|tsv|json|yaml|yml|zip|tar|gz|tgz|xlsx|xls|docx|pptx|md|txt|log|html|xml)\\b|\\.(?:pdf|png|jpg|jpeg|gif|webp|svg|csv|tsv|json|yaml|yml|zip|tar|gz|tgz|xlsx|xls|docx|pptx|md|txt|log|html|xml)\\b|\\b(?:outbox)\\b)",
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
FILES_HINT_TEMPLATE = """Inbox: {inbox}
Outbox (pending): {outbox}
Topic key: {topic_key}
Topic dir: {topic_dir}
Place files in outbox pending to send after this turn finishes.
Check delivery with /files outbox.
Max file size: {max_bytes} bytes."""


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


_GENERIC_TELEGRAM_ERRORS = {
    "Telegram request failed",
    "Telegram file download failed",
    "Telegram API returned error",
}


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
        return f"{cleaned[:limit - 3]}..."
    return cleaned


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


class TelegramCommandHandlers(
    WorkspaceCommands,
    GitHubCommands,
    FilesCommands,
    VoiceCommands,
    ExecutionCommands,
    ApprovalsCommands,
    FormattingHelpers,
):

    async def _handle_help(
        self, message: TelegramMessage, _args: str, _runtime: Any
    ) -> None:
        await self._send_message(
            message.chat_id,
            _format_help_text(self._command_specs),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_command(
        self, command: TelegramCommand, message: TelegramMessage, runtime: Any
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
                message.chat_id, outcome.transcript_message_id, outcome.transcript_text
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
            if pending.topic_key == key
            or pending.topic_key is None
            and pending.chat_id == chat_id
            and pending.thread_id == thread_id
        ]
        pending_question_ids = [
            request_id
            for request_id, pending in self._pending_questions.items()
            if pending.topic_key == key
            or pending.topic_key is None
            and pending.chat_id == chat_id
            and pending.thread_id == thread_id
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
                    chat_id, " ".join(parts), thread_id=thread_id, reply_to=reply_to
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
                self._interrupt_timeout_check(key, turn_id, response_message_id)
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
                    record, agent=agent, client=client, list_params=list_params
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
                items=items, options={option.model_id: option for option in options}
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
                    record, agent=agent, client=client, list_params=list_params
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
                record, model, effort=effort, clear_effort=not supports_effort
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
            turn_key: Optional[TurnKey] = None
            try:
                queue_wait_ms = int((time.monotonic() - queue_started_at) * 1000)
                log_event(
                    self._logger,
                    logging.INFO,
                    "telegram.review.queue_wait",
                    topic_key=await self._resolve_topic_key(message.chat_id, message.thread_id),
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
                review_kwargs["target"] = target
                review_kwargs["delivery"] = delivery
                turn_handle = await client.turn(
                    thread_id=thread_id,
                    turn_id=None,
                    turn_key=None,
                    prompt="",
                    input_items=[],
                    **review_kwargs,
                )
                turn_id = turn_handle.turn_id
                turn_key = TurnKey(
                    workspace_root=str(record.workspace_path),
                    thread_id=thread_id,
                    turn_id=turn_id,
                )
                turn_started_at = time.monotonic()
                log_event(
                    self._logger,
                    logging.INFO,
                    "telegram.review.started",
                    topic_key=await self._resolve_topic_key(message.chat_id, message.thread_id),
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    codex_thread_id=thread_id,
                    turn_id=turn_id,
                    turn_key=turn_key,
                    target=target.get("type"),
                    delivery=delivery,
                    review_kwargs=review_kwargs,
                    turn_started_at=now_iso(),
                )
                await turn_handle.wait(timeout=None)
                turn_elapsed_seconds = time.monotonic() - turn_started_at
                log_event(
                    self._logger,
                    logging.INFO,
                    "telegram.review.completed",
                    topic_key=await self._resolve_topic_key(message.chat_id, message.thread_id),
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    codex_thread_id=thread_id,
                    turn_id=turn_id,
                    turn_key=turn_key,
                    turn_elapsed_seconds=turn_elapsed_seconds,
                    turn_completed_at=now_iso(),
                )
            finally:
                try:
                    await self._send_placeholder(
                        message.chat_id,
                        thread_id=message.thread_id,
                        reply_to=message.message_id,
                        text=PLACEHOLDER_TEXT,
                    )
                    if placeholder_id is not None:
                        await self._delete_message(message.chat_id, placeholder_id)
                except Exception as exc:
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "telegram.review.placeholder_cleanup.failed",
                        chat_id=message.chat_id,
                        thread_id=message.thread_id,
                        exc=exc,
                    )
        except asyncio.CancelledError:
            log_event(
                self._logger,
                logging.INFO,
                "telegram.review.cancelled",
                topic_key=await self._resolve_topic_key(message.chat_id, message.thread_id),
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                codex_thread_id=thread_id,
                turn_id=turn_handle.turn_id if turn_handle else None,
                turn_key=turn_key,
                turn_cancelled_at=now_iso(),
            )
            await self._send_placeholder(
                message.chat_id,
                thread_id=message.thread_id,
                reply_to=message.message_id,
                text=PLACEHOLDER_TEXT,
            )
            if placeholder_id is not None:
                await self._delete_message(message.chat_id, placeholder_id)
            raise
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.review.failed",
                topic_key=await self._resolve_topic_key(message.chat_id, message.thread_id),
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                codex_thread_id=thread_id,
                turn_id=turn_handle.turn_id if turn_handle else None,
                turn_key=turn_key,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                "Review failed; check logs for details.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            try:
                await self._send_placeholder(
                    message.chat_id,
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                    text=PLACEHOLDER_TEXT,
                )
                if placeholder_id is not None:
                    await self._delete_message(message.chat_id, placeholder_id)
            except Exception as exc2:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.review.placeholder_cleanup.failed",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    exc=exc2,
                )
            return

    async def _opencode_review_arguments(target: dict[str, Any]) -> str:
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
            paths = target.get("paths")
            if isinstance(paths, list) and paths:
                return " ".join(paths)
        return ""

    async def _handle_review(
        self, message: TelegramMessage, args: str, runtime: Any
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
        agent = self._effective_agent(record)
        if agent != "codex":
            await self._send_message(
                message.chat_id,
                "Review is only available with the codex agent. Use /agent codex.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        argv = self._parse_command_args(args)
        if len(argv) > 1:
            await self._send_message(
                message.chat_id,
                "Usage: /review [target_type] [target_arg]",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        target_type = "uncommittedChanges"
        target: dict[str, Any] = {"type": target_type}
        if len(argv) == 1:
            arg = argv[0]
            if arg.lower() in ("branch", "basebranch", "base"):
                target_type = "baseBranch"
                target = {"type": target_type, "branch": "main"}
            elif arg.lower() == "commit":
                target_type = "commit"
                target = {"type": target_type, "sha": "HEAD"}
            else:
                target_type = "custom"
                target = {"type": target_type, "paths": [arg]}
        review_arguments = await self._opencode_review_arguments(target)
        if review_arguments:
            prompt_text = (
                f"Review this: {target_type} {review_arguments}\n\n"
                "Please provide your feedback on the changes. "
                "Focus on code quality, potential bugs, and suggestions for improvement."
            )
        else:
            prompt_text = (
                f"Review uncommitted changes\n\n"
                "Please provide your feedback on the changes. "
                "Focus on code quality, potential bugs, and suggestions for improvement."
            )
        delivery = "telegram"
        await self._start_codex_review(
            message,
            runtime,
            record=record,
            thread_id=record.active_thread_id,
            target=target,
            delivery=delivery,
        )

    async def _handle_skills(
        self, message: TelegramMessage, _args: str, _runtime: Any
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
                                        context_model_payload = (
                                            model_payload
                                            or _extract_model_ids_from_part(part)
                                        )
                                        opencode_context_window = await self._resolve_opencode_model_context_window(
                                            opencode_client,
                                            workspace_root,
                                            context_model_payload,
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
            f"Detected GitHub issue: {slug}#{number}\nStart PR flow to create a PR?",
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
                "skills/list", {"cwds": [record.workspace_path], "forceReload": False}
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
                result = await client.request("config/read", {"includeLayers": False})
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
                {"keyPath": key_path, "value": value, "mergeStrategy": "replace"},
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
            message, runtime, text_override=INIT_PROMPT, record=record
        )

    async def _apply_compact_summary(
        self, message: TelegramMessage, record: "TelegramTopicRecord", summary_text: str
    ) -> tuple[bool, str | None]:
        if not record.workspace_path:
            return (False, "Topic not bound. Use /bind <repo_id> or /bind <path>.")
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
            return (False, "Topic not bound. Use /bind <repo_id> or /bind <path>.")
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
            message.chat_id, message.thread_id, thread, active_thread_id=new_thread_id
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
                message, runtime, text_override=COMPACT_SUMMARY_PROMPT, record=record
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
                message, record.active_thread_id, conflict_key
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
            message, summary_text, reply_markup=reply_markup
        )
        if outcome.turn_id:
            self._token_usage_by_turn.pop(outcome.turn_id, None)
        await self._delete_message(message.chat_id, outcome.placeholder_id)
        await self._finalize_voice_transcript(
            message.chat_id, outcome.transcript_message_id, outcome.transcript_text
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
        self, key: str, callback: TelegramCallbackQuery, parsed: CompactCallback
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
                    callback.chat_id, text, thread_id=callback.thread_id
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
                    f"""{state.display_text}

Compact canceled.""",
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
            f"""{state.display_text}

Applying summary...""",
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
            message, record, state.summary_text
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
                f"""{state.display_text}

Failed to start new thread with summary.""",
                reply_markup=None,
            )
            if edited:
                self._mark_compact_notified(status)
            elif await _send_compact_status("Failed to start new thread with summary."):
                self._mark_compact_notified(status)
            if failure_message:
                await self._send_message(
                    callback.chat_id, failure_message, thread_id=callback.thread_id
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
            f"""{state.display_text}

Summary applied.""",
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
                    chat_id, failure, thread_id=thread_id, reply_to=reply_to
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
                chat_id, message, thread_id=thread_id, reply_to=reply_to
            )
        self._schedule_update_status_watch(chat_id, thread_id)

    async def _prompt_update_selection(
        self, message: TelegramMessage, *, prompt: str = UPDATE_PICKER_PROMPT
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
                f"""{display_text}

{message}""",
                reply_markup=None,
            )
            sent = edited
        if not sent and message:
            try:
                await self._send_message(
                    chat_id, message, thread_id=thread_id, reply_to=message_id
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
