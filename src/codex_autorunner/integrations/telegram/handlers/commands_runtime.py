from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import secrets
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
from ....core.update_paths import resolve_update_paths
from ....core.utils import canonicalize_path
from ...app_server.client import _normalize_sandbox_policy
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
    MAX_MENTION_BYTES,
    MODEL_PICKER_PROMPT,
    SHELL_MESSAGE_BUFFER_CHARS,
    TELEGRAM_MAX_MESSAGE_LENGTH,
    THREAD_LIST_MAX_PAGES,
    THREAD_LIST_PAGE_LIMIT,
    UPDATE_PICKER_PROMPT,
    UPDATE_TARGET_OPTIONS,
    VALID_REASONING_EFFORTS,
    TurnKey,
)
from ..helpers import (
    CodexFeatureRow,
    _coerce_model_options,
    _compact_preview,
    _extract_command_result,
    _extract_rollout_path,
    _extract_thread_id,
    _extract_thread_info,
    _find_thread_entry,
    _format_feature_flags,
    _format_help_text,
    _format_mcp_list,
    _format_model_list,
    _format_shell_body,
    _format_skills_list,
    _looks_binary,
    _parse_review_commit_log,
    _path_within,
    _prepare_shell_response,
    _render_command_output,
    _set_model_overrides,
    _set_pending_compact_seed,
    _set_rollout_path,
    _thread_summary_preview,
    _with_conversation_id,
    derive_codex_features_command,
    format_codex_features,
    parse_codex_features_list,
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
from .utils import _coerce_int

if TYPE_CHECKING:
    from ..state import TelegramTopicRecord

from .commands import (
    ApprovalsCommands,
    ExecutionCommands,
    FilesCommands,
    FlowCommands,
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
        return f"{cleaned[: limit - 3]}..."
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


class TelegramCommandHandlers(
    WorkspaceCommands,
    GitHubCommands,
    FlowCommands,
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

        async def _read_explicit_config_features() -> Optional[str]:
            try:
                result = await client.request("config/read", {"includeLayers": False})
            except Exception:
                return None
            return _format_feature_flags(result)

        async def _fetch_codex_features() -> (
            tuple[list[CodexFeatureRow], Optional[str]]
        ):
            features_command = derive_codex_features_command(
                self._config.app_server_command
            )
            try:
                result = await client.request(
                    "command/exec",
                    {
                        "cwd": record.workspace_path,
                        "command": features_command,
                        "timeoutMs": 10000,
                    },
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.experimental.exec_failed",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    exc=exc,
                )
                return (
                    [],
                    "Failed to run `codex features list`; check Codex install/PATH.",
                )
            stdout, stderr, exit_code = _extract_command_result(result)
            if exit_code not in (None, 0):
                detail = stderr.strip() if isinstance(stderr, str) else ""
                msg = f"`{' '.join(features_command)}` failed (exit {exit_code})."
                if detail:
                    msg = f"{msg} stderr: {detail}"
                return [], msg
            rows = parse_codex_features_list(stdout)
            if not rows:
                return (
                    [],
                    f"No feature rows returned by `{' '.join(features_command)}`.",
                )
            return rows, None

        list_all = bool(argv and argv[0].lower() == "all")
        is_list_request = not argv or list_all or argv[0].lower() in ("list", "ls")
        if is_list_request:
            stage_filter = None if list_all else "beta"
            rows, error = await _fetch_codex_features()
            if error:
                fallback = await _read_explicit_config_features()
                message_lines = [error]
                if fallback and fallback.strip() != "No feature flags found.":
                    message_lines.append("")
                    message_lines.append("Explicit config entries (may be incomplete):")
                    message_lines.append(fallback)
                await self._send_message(
                    message.chat_id,
                    "\n".join(message_lines),
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            await self._send_message(
                message.chat_id,
                format_codex_features(rows, stage_filter=stage_filter),
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

        rows, error = await _fetch_codex_features()
        if error:
            await self._send_message(
                message.chat_id,
                error,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        normalized_feature = feature
        if feature.startswith("features."):
            normalized_feature = feature[len("features.") :]
        target_row = next((row for row in rows if row.key == normalized_feature), None)
        if target_row is None:
            available = ", ".join(sorted(row.key for row in rows))
            await self._send_message(
                message.chat_id,
                f"Unknown feature '{feature}'. Known features: {available}\n"
                "Use /experimental all to list all stages.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        key_path = feature if feature.startswith("features.") else f"features.{feature}"
        try:
            write_result = await client.request(
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

        post_rows, post_error = await _fetch_codex_features()
        effective_row = None
        if not post_error:
            effective_row = next(
                (row for row in post_rows if row.key == normalized_feature), None
            )

        lines = [f"Feature {key_path} set to {value}."]
        if effective_row:
            lines.append(
                f"Stage: {effective_row.stage}; effective state: {effective_row.enabled}."
            )
        elif post_error:
            lines.append(f"(Could not verify effective state: {post_error})")

        if isinstance(write_result, dict):
            status = write_result.get("status")
            overridden = write_result.get("overriddenMetadata")
            if status == "okOverridden" and isinstance(overridden, dict):
                message_txt = overridden.get("message")
                effective_value = overridden.get("effectiveValue")
                layer = overridden.get("overridingLayer") or {}
                layer_name = layer.get("name") if isinstance(layer, dict) else None
                layer_version = (
                    layer.get("version") if isinstance(layer, dict) else None
                )
                lines.append("Write was overridden by another config layer.")
                if layer_name:
                    layer_desc = (
                        f"{layer_name} (version {layer_version})"
                        if layer_version
                        else layer_name
                    )
                    lines.append(f"- Overriding layer: {layer_desc}")
                if effective_value is not None:
                    lines.append(f"- Effective value: {effective_value}")
                if isinstance(message_txt, str) and message_txt:
                    lines.append(f"- Note: {message_txt}")

        await self._send_message(
            message.chat_id,
            "\n".join(lines),
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
        update_dir = resolve_update_paths().cache_dir
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
                skip_checks=bool(getattr(self, "_update_skip_checks", False)),
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
        return resolve_update_paths().status_path

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
        return resolve_update_paths().compact_status_path

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
