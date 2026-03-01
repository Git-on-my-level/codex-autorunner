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
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from ....agents.opencode.supervisor import OpenCodeSupervisorError
from ....core.logging_utils import log_event
from ....core.pma_delivery_targets import PmaDeliveryTargetsStore, target_key
from ....core.pma_target_refs import parse_pma_target_ref, pma_targets_usage
from ....core.state import now_iso
from ....core.update import (
    _available_update_target_options,
    _normalize_update_target,
    _spawn_update_process,
)
from ....core.update_paths import resolve_update_paths
from ....core.utils import canonicalize_path
from ...app_server.client import _normalize_sandbox_policy
from ...chat.media import (
    format_media_batch_failure as _format_media_batch_failure,  # noqa: F401
)
from ...chat.update_notifier import (
    format_update_status_message,
    mark_update_status_notified,
)
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
    MODEL_CATALOG_TTL_SECONDS,
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

_COMMAND_ALIASES = {
    "models": "model",
}


@dataclass
class _RuntimeStub:
    current_turn_id: Optional[str] = None
    current_turn_key: Optional[TurnKey] = None
    interrupt_requested: bool = False
    interrupt_message_id: Optional[int] = None
    interrupt_turn_id: Optional[str] = None


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
        original_name = command.name
        name = _COMMAND_ALIASES.get(original_name, original_name)
        args = command.args
        log_event(
            self._logger,
            logging.INFO,
            "telegram.command",
            name=name,
            original_name=original_name,
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
        record = await self._router.ensure_topic(message.chat_id, message.thread_id)
        agent = self._effective_agent(record)
        supports_effort = self._agent_supports_effort(agent)
        list_params = {
            "cursor": None,
            "limit": DEFAULT_MODEL_LIST_LIMIT,
            "agent": agent,
        }
        workspace_path, error = self._resolve_workspace_path(record, allow_pma=True)
        if workspace_path is None:
            await self._send_message(
                message.chat_id,
                error or "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        try:
            client = await self._client_for_workspace(workspace_path)
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
                error or "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        argv = self._parse_command_args(args)
        cache_key = f"{workspace_path}:{agent}"
        if argv and argv[0].lower() in ("list", "ls"):
            record_for_models = self._record_with_workspace_path(record, workspace_path)
            cached_result = self._model_catalog_cache.get(cache_key)
            if cached_result is not None:
                result, cached_time = cached_result
                if time.monotonic() - cached_time < MODEL_CATALOG_TTL_SECONDS:
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
            try:
                result = await self._fetch_model_list(
                    record_for_models,
                    agent=agent,
                    client=client,
                    list_params=list_params,
                )
                self._model_catalog_cache[cache_key] = (result, time.monotonic())
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
        search_query = argv[0].lower() if argv else None
        cached_result = self._model_catalog_cache.get(cache_key)
        if cached_result is not None:
            result, cached_time = cached_result
            if time.monotonic() - cached_time < MODEL_CATALOG_TTL_SECONDS:
                options = _coerce_model_options(result, include_efforts=supports_effort)
                if search_query:
                    options = [
                        opt
                        for opt in options
                        if search_query in opt.model_id.lower()
                        or search_query in opt.label.lower()
                    ]
                if not options:
                    await self._send_message(
                        message.chat_id,
                        f"No models found matching '{search_query}'.",
                        thread_id=message.thread_id,
                        reply_to=message.message_id,
                    )
                    return
                items = [(option.model_id, option.model_id) for option in options]
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
                prompt_text = f"Models matching '{search_query}'"
                if search_query:
                    prompt_text = (
                        f"Search: '{search_query}' (showing {len(options)} results)"
                    )
                await self._send_message(
                    message.chat_id,
                    prompt_text,
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                    reply_markup=keyboard,
                )
                return
        if not argv:
            record_for_models = self._record_with_workspace_path(record, workspace_path)
            try:
                result = await self._fetch_model_list(
                    record_for_models,
                    agent=agent,
                    client=client,
                    list_params=list_params,
                )
                self._model_catalog_cache[cache_key] = (result, time.monotonic())
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

    async def _handle_pma(
        self, message: TelegramMessage, args: str, _runtime: Any
    ) -> None:
        if not self._hub_root:
            await self._send_message(
                message.chat_id,
                "PMA unavailable; hub root not configured.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        supervisor = getattr(self, "_hub_supervisor", None)
        if supervisor and hasattr(supervisor, "hub_config"):
            pma_config = supervisor.hub_config.pma
            if not pma_config.enabled:
                await self._send_message(
                    message.chat_id,
                    "PMA is disabled in hub config. Set pma.enabled: true to enable.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return

        argv = self._parse_command_args(args)
        action = argv[0].lower() if argv else ""
        if action == "targets":
            await self._handle_pma_targets_list(message)
            return
        if action == "target":
            await self._handle_pma_target_mutation(message, argv[1:])
            return

        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        record = await self._router.get_topic(key)
        current = bool(record and record.pma_enabled)
        if action in ("on", "enable", "true"):
            enabled = True
        elif action in ("off", "disable", "false"):
            enabled = False
        elif action in ("status", "show"):
            await self._send_message(
                message.chat_id,
                f"PMA mode: {'enabled' if current else 'disabled'}",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        elif action:
            await self._send_message(
                message.chat_id,
                self._pma_usage(),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        else:
            enabled = not current

        if record is None:
            record = await self._router.ensure_topic(message.chat_id, message.thread_id)

        def apply_pma(record: TelegramTopicRecord) -> None:
            record.pma_enabled = enabled
            if enabled:
                # Save previous binding before entering PMA mode.
                record.pma_prev_repo_id = record.repo_id
                record.pma_prev_workspace_path = record.workspace_path
                record.pma_prev_workspace_id = record.workspace_id
                record.pma_prev_active_thread_id = record.active_thread_id
                # Mutual exclusion: PMA mode implies Hub context, so unbind specific repo.
                record.workspace_path = None
                record.repo_id = None
                record.workspace_id = None
                record.active_thread_id = None
            else:
                # Restore previous binding when exiting PMA mode.
                if record.pma_prev_repo_id or record.pma_prev_workspace_path:
                    record.repo_id = record.pma_prev_repo_id
                    record.workspace_path = record.pma_prev_workspace_path
                    record.workspace_id = record.pma_prev_workspace_id
                    record.active_thread_id = record.pma_prev_active_thread_id
                    # Clear saved previous binding after restore.
                    record.pma_prev_repo_id = None
                    record.pma_prev_workspace_path = None
                    record.pma_prev_workspace_id = None
                    record.pma_prev_active_thread_id = None

        await self._router.update_topic(
            message.chat_id,
            message.thread_id,
            apply_pma,
        )

        try:
            if enabled:
                targets_store = PmaDeliveryTargetsStore(Path(self._hub_root))
                here_target = self._pma_here_target(message)
                state = targets_store.load()
                current_targets = [
                    target
                    for target in state.get("targets", [])
                    if isinstance(target, dict)
                ]
                if len(current_targets) > 1:
                    targets_store.add_target(here_target)
                else:
                    targets_store.set_targets([here_target])
            # /pma off should not clear delivery targets.
        except Exception:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.pma.delivery_targets.update_failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                enabled=enabled,
            )

        status = "enabled" if enabled else "disabled"
        if enabled:
            hint = "Use /pma off to exit. Previous repo binding saved."
        else:
            previous = (record and record.pma_prev_workspace_path) or None
            if previous:
                hint = f"Back to repo mode. Restored {previous}."
            else:
                hint = "Back to repo mode."
        await self._send_message(
            message.chat_id,
            f"PMA mode {status}. {hint}",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_pma_targets_list(self, message: TelegramMessage) -> None:
        targets_store = PmaDeliveryTargetsStore(Path(self._hub_root))
        state = targets_store.load()
        active_key = targets_store.get_active_target_key()
        targets = [
            target for target in state.get("targets", []) if isinstance(target, dict)
        ]
        if not targets:
            text = "PMA delivery targets: (none)"
        else:
            lines = ["PMA delivery targets:"]
            for target in targets:
                key = target_key(target)
                if not isinstance(key, str):
                    continue
                label = self._format_pma_target_label(target)
                active_suffix = " [active]" if key == active_key else ""
                if label:
                    lines.append(f"- {key} ({label}){active_suffix}")
                else:
                    lines.append(f"- {key}{active_suffix}")
            text = "\n".join(lines)
        await self._send_message(
            message.chat_id,
            text,
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_pma_target_mutation(
        self, message: TelegramMessage, argv: list[str]
    ) -> None:
        if not argv:
            await self._send_message(
                message.chat_id,
                self._pma_usage(),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        action = argv[0].lower()
        targets_store = PmaDeliveryTargetsStore(Path(self._hub_root))

        if action == "clear":
            targets_store.set_targets([])
            await self._send_message(
                message.chat_id,
                "Cleared PMA delivery targets.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        if action == "active":
            if len(argv) < 2:
                await self._send_message(
                    message.chat_id,
                    self._format_pma_active_target(targets_store),
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            subaction = argv[1].strip().lower()
            if subaction in {"show", "status"} and len(argv) == 2:
                await self._send_message(
                    message.chat_id,
                    self._format_pma_active_target(targets_store),
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            if subaction == "set":
                ref_or_key = " ".join(argv[2:]).strip()
            else:
                ref_or_key = " ".join(argv[1:]).strip()
            key, error = self._resolve_pma_target_key_for_active(
                message=message,
                targets_store=targets_store,
                ref_or_key=ref_or_key,
            )
            if error:
                await self._send_message(
                    message.chat_id,
                    error,
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            if not isinstance(key, str):
                await self._send_message(
                    message.chat_id,
                    "Unable to resolve target key.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            changed = targets_store.set_active_target(key)
            text = (
                f"Set active PMA delivery target: {key}"
                if changed
                else f"PMA delivery target already active: {key}"
            )
            await self._send_message(
                message.chat_id,
                text,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        if action not in {"add", "rm", "remove"} or len(argv) < 2:
            await self._send_message(
                message.chat_id,
                self._pma_usage(),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        ref = " ".join(argv[1:]).strip()
        target = self._parse_pma_target_ref(message, ref)
        if target is None:
            await self._send_message(
                message.chat_id,
                f"Invalid target ref '{ref}'.\n{self._pma_usage()}",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        key = target_key(target)
        if not isinstance(key, str):
            await self._send_message(
                message.chat_id,
                f"Invalid target ref '{ref}'.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if action == "add":
            targets_store.add_target(target)
            await self._send_message(
                message.chat_id,
                f"Added PMA delivery target: {key}",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        removed = targets_store.remove_target(target)
        await self._send_message(
            message.chat_id,
            (
                f"Removed PMA delivery target: {key}"
                if removed
                else f"PMA delivery target not found: {key}"
            ),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    def _pma_usage(self) -> str:
        return "\n".join(
            [
                "Usage:",
                "/pma [on|off|status|targets]",
                "/pma target add <ref>",
                "/pma target rm <ref>",
                "/pma target clear",
                "/pma target active [show|set <ref|key>]",
                pma_targets_usage(include_here=True),
            ]
        )

    def _format_pma_active_target(self, targets_store: PmaDeliveryTargetsStore) -> str:
        active_key = targets_store.get_active_target_key()
        if not isinstance(active_key, str):
            return (
                "Active PMA delivery target: (not set; use /pma target active set "
                "<ref|key>)"
            )
        state = targets_store.load()
        active_target = next(
            (
                target
                for target in state.get("targets", [])
                if isinstance(target, dict) and target_key(target) == active_key
            ),
            None,
        )
        label = (
            self._format_pma_target_label(active_target)
            if isinstance(active_target, dict)
            else ""
        )
        if label:
            return f"Active PMA delivery target: {active_key} ({label})"
        return f"Active PMA delivery target: {active_key}"

    def _resolve_pma_target_key_for_active(
        self,
        *,
        message: TelegramMessage,
        targets_store: PmaDeliveryTargetsStore,
        ref_or_key: str,
    ) -> tuple[Optional[str], Optional[str]]:
        candidate = ref_or_key.strip()
        if not candidate:
            return None, self._pma_usage()

        state = targets_store.load()
        known_keys = {
            key
            for key in (target_key(target) for target in state.get("targets", []))
            if isinstance(key, str)
        }
        target = self._parse_pma_target_ref(message, candidate)
        if target is None:
            if candidate in known_keys:
                return candidate, None
            return None, f"Invalid target ref '{candidate}'.\n{self._pma_usage()}"

        key = target_key(target)
        if not isinstance(key, str):
            return None, f"Invalid target ref '{candidate}'."
        if key not in known_keys:
            return None, f"PMA delivery target not found: {key}"
        return key, None

    def _pma_here_target(self, message: TelegramMessage) -> dict[str, Any]:
        target: dict[str, Any] = {
            "kind": "chat",
            "platform": "telegram",
            "chat_id": str(message.chat_id),
            "conversation_key": topic_key(message.chat_id, message.thread_id),
        }
        if message.thread_id is not None:
            target["thread_id"] = str(message.thread_id)
        return target

    def _parse_pma_target_ref(
        self, message: TelegramMessage, ref: str
    ) -> Optional[dict[str, Any]]:
        return parse_pma_target_ref(ref, here_target=self._pma_here_target(message))

    def _format_pma_target_label(self, target: dict[str, Any]) -> str:
        label = target.get("label")
        if isinstance(label, str) and label.strip():
            return label.strip()
        kind = target.get("kind")
        if kind == "chat":
            platform = target.get("platform")
            chat_id = target.get("chat_id")
            thread_id = target.get("thread_id")
            if (
                isinstance(platform, str)
                and isinstance(chat_id, str)
                and platform
                and chat_id
            ):
                if isinstance(thread_id, str) and thread_id:
                    return f"{platform} {chat_id} thread {thread_id}"
                return f"{platform} {chat_id}"
        if kind == "local":
            path = target.get("path")
            if isinstance(path, str) and path:
                return path
        return ""

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
        workspace_path, error = self._resolve_workspace_path(record, allow_pma=True)
        if not workspace_path:
            return (
                False,
                error or "Topic not bound. Use /bind <repo_id> or /bind <path>.",
            )
        try:
            client = await self._client_for_workspace(workspace_path)
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
            return (
                False,
                error or "Topic not bound. Use /bind <repo_id> or /bind <path>.",
            )
        log_event(
            self._logger,
            logging.INFO,
            "telegram.compact.apply.start",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            summary_len=len(summary_text),
            workspace_path=workspace_path,
        )
        try:
            agent = self._effective_agent(record)
            thread = await client.thread_start(workspace_path, agent=agent)
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
            message, workspace_path, thread, action="thread_start"
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
            record = await self._require_bound_record(message, allow_pma=True)
            if not record:
                return
            await self._handle_normal_message(
                message, runtime, text_override=COMPACT_SUMMARY_PROMPT, record=record
            )
            return
        auto_apply = bool(argv and argv[0].lower() == "apply")
        record = await self._require_bound_record(message, allow_pma=True)
        if not record:
            return
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        pma_enabled = bool(record.pma_enabled)
        if pma_enabled:
            registry = getattr(self, "_hub_thread_registry", None)
            pma_key = self._pma_registry_key(record, message)
            pma_thread_id = (
                registry.get_thread_id(pma_key)
                if registry is not None and pma_key
                else None
            )
            if not pma_thread_id:
                await self._send_message(
                    message.chat_id,
                    "No active PMA thread to compact. Send a message or use /new to start one.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
        else:
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
        if record is None:
            await self._answer_callback(callback, "Selection expired")
            return
        workspace_path, error = self._resolve_workspace_path(record, allow_pma=True)
        if workspace_path is None:
            await self._answer_callback(callback, error or "Selection expired")
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
        update_backend = str(getattr(self, "_update_backend", "auto") or "auto")
        update_services = getattr(self, "_update_linux_service_names", None)
        linux_hub_service_name: Optional[str] = None
        linux_telegram_service_name: Optional[str] = None
        linux_discord_service_name: Optional[str] = None
        if isinstance(update_services, dict):
            hub_service = update_services.get("hub")
            telegram_service = update_services.get("telegram")
            discord_service = update_services.get("discord")
            if isinstance(hub_service, str) and hub_service.strip():
                linux_hub_service_name = hub_service.strip()
            if isinstance(telegram_service, str) and telegram_service.strip():
                linux_telegram_service_name = telegram_service.strip()
            if isinstance(discord_service, str) and discord_service.strip():
                linux_discord_service_name = discord_service.strip()
        notify_reply_to = reply_to
        if notify_reply_to is None and callback is not None:
            notify_reply_to = callback.message_id
        notify_metadata = self._update_status_notifier.build_spawn_metadata(
            chat_id=chat_id,
            thread_id=thread_id,
            reply_to=notify_reply_to,
            include_legacy_telegram_keys=True,
        )
        try:
            _spawn_update_process(
                repo_url=repo_url,
                repo_ref=repo_ref,
                update_dir=update_dir,
                logger=self._logger,
                update_target=update_target,
                skip_checks=bool(getattr(self, "_update_skip_checks", False)),
                update_backend=update_backend,
                linux_hub_service_name=linux_hub_service_name,
                linux_telegram_service_name=linux_telegram_service_name,
                linux_discord_service_name=linux_discord_service_name,
                **notify_metadata,
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
        self._schedule_update_status_watch(chat_id, thread_id, reply_to=notify_reply_to)

    async def _prompt_update_selection(
        self, message: TelegramMessage, *, prompt: str = UPDATE_PICKER_PROMPT
    ) -> None:
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        state = SelectionState(items=list(self._dynamic_update_target_options()))
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
        state = SelectionState(items=list(self._dynamic_update_target_options()))
        keyboard = self._build_update_keyboard(state)
        self._update_options[key] = state
        self._touch_cache_timestamp("update_options", key)
        await self._update_selection_message(key, callback, prompt, keyboard)

    def _dynamic_update_target_options(self) -> tuple[tuple[str, str], ...]:
        update_backend = str(getattr(self, "_update_backend", "auto") or "auto")
        update_services = getattr(self, "_update_linux_service_names", None)
        raw_config: Optional[dict[str, Any]] = None

        supervisor = getattr(self, "_hub_supervisor", None)
        if supervisor and hasattr(supervisor, "hub_config"):
            hub_config = getattr(supervisor, "hub_config", None)
            if hub_config is not None:
                raw = getattr(hub_config, "raw", None)
                if isinstance(raw, dict):
                    raw_config = raw
                backend = getattr(hub_config, "update_backend", None)
                if isinstance(backend, str) and backend.strip():
                    update_backend = backend.strip()
                services = getattr(hub_config, "update_linux_service_names", None)
                if isinstance(services, dict):
                    update_services = services

        options = _available_update_target_options(
            raw_config=raw_config,
            update_backend=update_backend,
            linux_service_names=(
                update_services if isinstance(update_services, dict) else None
            ),
        )
        if options:
            return options
        return UPDATE_TARGET_OPTIONS

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
        return format_update_status_message(status)

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
        reply_to: Optional[int] = None,
        timeout_seconds: float = 300.0,
        interval_seconds: float = 2.0,
    ) -> None:
        notify_context: dict[str, Any] = {
            "chat_id": chat_id,
            "thread_id": thread_id,
        }
        if reply_to is not None:
            notify_context["reply_to"] = reply_to
        self._update_status_notifier.schedule_watch(
            notify_context,
            timeout_seconds=timeout_seconds,
            interval_seconds=interval_seconds,
        )

    async def _send_update_status_notice(
        self, notify_context: dict[str, Any], text: str
    ) -> None:
        chat_id = notify_context.get("chat_id")
        if not isinstance(chat_id, int) or isinstance(chat_id, bool):
            return
        thread_id = notify_context.get("thread_id")
        if not isinstance(thread_id, int) or isinstance(thread_id, bool):
            thread_id = None
        reply_to = notify_context.get("reply_to")
        if not isinstance(reply_to, int) or isinstance(reply_to, bool):
            reply_to = None
        await self._send_message(
            chat_id,
            text,
            thread_id=thread_id,
            reply_to=reply_to,
        )

    def _mark_update_notified(self, status: dict[str, Any]) -> None:
        mark_update_status_notified(
            path=self._update_status_path(),
            status=status,
            logger=self._logger,
            log_event_name="telegram.update.notify_write_failed",
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
        await self._update_status_notifier.maybe_send_notice()

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
        record = await self._require_bound_record(message, allow_pma=True)
        if not record:
            return
        workspace_path, error = self._resolve_workspace_path(record, allow_pma=True)
        if workspace_path is None:
            await self._send_message(
                message.chat_id,
                error or "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        client = await self._client_for_workspace(workspace_path)
        if client is None:
            await self._send_message(
                message.chat_id,
                error or "Topic not bound. Use /bind <repo_id> or /bind <path>.",
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
        record = await self._require_bound_record(message, allow_pma=True)
        if not record:
            return
        workspace_path, error = self._resolve_workspace_path(record, allow_pma=True)
        if workspace_path is None:
            await self._send_message(
                message.chat_id,
                error or "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        client = await self._client_for_workspace(workspace_path)
        if client is None:
            await self._send_message(
                message.chat_id,
                error or "Topic not bound. Use /bind <repo_id> or /bind <path>.",
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
