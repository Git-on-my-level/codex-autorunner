from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re
import shlex
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from ....agents.opencode.supervisor import OpenCodeSupervisorError
from ....core.coercion import coerce_int
from ....core.config import load_hub_config
from ....core.logging_utils import log_event
from ....core.state import now_iso
from ....core.update import (
    _available_update_target_options,
    _format_update_confirmation_warning,
    _normalize_update_target,
    _spawn_update_process,
    _update_target_restarts_surface,
)
from ....core.update_paths import resolve_update_paths
from ....core.update_targets import get_update_target_label
from ...app_server.client import CodexAppServerError
from ...chat.chat_ux_telemetry import (
    ChatUxMilestone,
)
from ...chat.chat_ux_telemetry import (
    ChatUxTimingSnapshot as _Snap,
)
from ...chat.constants import (
    APP_SERVER_UNAVAILABLE_MESSAGE,
    TOPIC_NOT_BOUND_MESSAGE,
)
from ...chat.interrupt_controller import (
    SharedInterruptState,
    render_managed_thread_interrupt_message,
    request_managed_thread_interrupt,
)
from ...chat.media import (
    format_media_batch_failure as _format_media_batch_failure,  # noqa: F401
)
from ...chat.picker_filter import (
    filter_picker_items,
    find_exact_picker_item,
)
from ...chat.session_messages import (
    format_update_started_message,
    format_update_status_message,
)
from ...chat.status_diagnostics import build_process_monitor_lines_for_root
from ...chat.turn_metrics import compose_turn_response_with_footer
from ...chat.update_notifier import mark_update_status_notified
from ..adapter import (
    CompactCallback,
    InlineButton,
    TelegramCallbackQuery,
    TelegramCommand,
    TelegramMessage,
    build_inline_keyboard,
    build_update_confirm_keyboard,
    encode_cancel_callback,
)
from ..collaboration_helpers import (
    build_collaboration_snippet_lines,
    collaboration_summary_lines,
    evaluate_collaboration_summary,
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
    MODEL_CATALOG_TTL_SECONDS,
    MODEL_PICKER_PROMPT,
    REASONING_EFFORT_VALUES,
    THREAD_LIST_MAX_PAGES,
    THREAD_LIST_PAGE_LIMIT,
    UPDATE_PICKER_PROMPT,
    UPDATE_TARGET_OPTIONS,
    VALID_REASONING_EFFORTS,
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
    _format_skills_list,
    _parse_review_commit_log,
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
    CompactStatusState,
    ModelPickerState,
    SelectionState,
    TelegramNoticeContext,
    UpdateConfirmState,
)

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
from .commands.shared import _RuntimeStub  # noqa: F401

PROMPT_CONTEXT_RE = re.compile("\\bprompt\\b", re.IGNORECASE)
PROMPT_CONTEXT_HINT = (
    "If the user asks to write a prompt, put the prompt in a ```code block```."
)
OUTBOX_CONTEXT_RE = re.compile(
    "(?:\\b(?:pdf|png|jpg|jpeg|gif|webp|svg|csv|tsv|json|yaml|yml|zip|tar|gz|tgz|xlsx|xls|docx|pptx|md|txt|log|html|xml)\\b|\\.(?:pdf|png|jpg|jpeg|gif|webp|svg|csv|tsv|json|yaml|yml|zip|tar|gz|tgz|xlsx|xls|docx|pptx|md|txt|log|html|xml)\\b|\\b(?:outbox)\\b)",
    re.IGNORECASE,
)

_COMMAND_ALIASES = {
    "models": "model",
}


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
        except (RuntimeError, OSError, ValueError, TypeError, ConnectionError):
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
                        value = coerce_int(limit.get(key))
                        if value is not None and value > 0:
                            context_window = value
                            break
                if context_window is None:
                    for key in _OPENCODE_MODEL_CONTEXT_KEYS:
                        value = coerce_int(model_entry.get(key))
                        if value is not None and value > 0:
                            context_window = value
                            break
            if context_window is None:
                limit = provider.get("limit") or provider.get("limits")
                if isinstance(limit, dict):
                    for key in _OPENCODE_MODEL_CONTEXT_KEYS:
                        value = coerce_int(limit.get(key))
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
        surface_key_override: Optional[str] = None,
        pma_context_prefix: Optional[str] = None,
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
            surface_key_override=surface_key_override,
            pma_context_prefix=pma_context_prefix,
        )
        if isinstance(outcome, _TurnRunFailure):
            return
        metrics = self._format_turn_metrics_text(
            outcome.token_usage, outcome.elapsed_seconds
        )
        metrics_mode = self._metrics_mode()
        overflow_mode_override = None
        resolved_agent = self._effective_agent(outcome.record)
        response_text = outcome.response
        intermediate_response = outcome.intermediate_response
        if resolved_agent == "opencode":
            if (
                isinstance(response_text, str)
                and response_text.strip() == "No response."
            ):
                response_text = ""
        if metrics_mode == "append_to_response":
            response_text = compose_turn_response_with_footer(
                response_text,
                summary_text=outcome.intermediate_response,
                token_usage=outcome.token_usage,
                elapsed_seconds=outcome.elapsed_seconds,
                agent=resolved_agent,
                model=getattr(outcome.record, "model", None),
            )
            intermediate_response = None
            if getattr(self._config, "message_overflow", "document") == "document":
                overflow_mode_override = "split"
        elif resolved_agent == "opencode":
            response_text = compose_turn_response_with_footer(
                response_text,
                summary_text=outcome.intermediate_response,
                token_usage=None,
                elapsed_seconds=None,
                agent=resolved_agent,
                model=getattr(outcome.record, "model", None),
            )
            intermediate_response = None
        try:
            response_sent = await self._deliver_turn_response(
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                reply_to=message.message_id,
                placeholder_id=outcome.placeholder_id,
                response=response_text,
                intermediate_response=intermediate_response,
                overflow_mode_override=overflow_mode_override,
            )
        except TypeError as exc:
            if not any(
                key in str(exc)
                for key in ("intermediate_response", "overflow_mode_override")
            ):
                raise
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
        interrupt_status_turn_id = getattr(outcome, "interrupt_status_turn_id", None)
        interrupt_status_fallback_text = getattr(
            outcome, "interrupt_status_fallback_text", None
        )
        if interrupt_status_fallback_text and interrupt_status_turn_id:
            await self._clear_interrupt_status_message(
                chat_id=message.chat_id,
                runtime=runtime,
                turn_id=interrupt_status_turn_id,
                fallback_text=interrupt_status_fallback_text,
                outcome_visible=response_sent,
            )
        if metrics and metrics_mode == "separate":
            await self._send_turn_metrics(
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                reply_to=message.message_id,
                elapsed_seconds=outcome.elapsed_seconds,
                token_usage=outcome.token_usage,
            )
        elif metrics and metrics_mode == "append_to_progress":
            await self._append_metrics_to_placeholder(
                message.chat_id,
                outcome.placeholder_id,
                metrics,
                base_text=(
                    outcome.intermediate_response
                    if isinstance(outcome.intermediate_response, str)
                    else None
                ),
            )
        if outcome.turn_id:
            self._token_usage_by_turn.pop(outcome.turn_id, None)
        if response_sent:
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

    async def _handle_queue_cancel_callback(
        self, callback: TelegramCallbackQuery, kind: str
    ) -> None:
        if callback.chat_id is None:
            await self._answer_callback(callback, "Queue item unavailable")
            return
        _prefix, _sep, source_message_id_raw = kind.partition(":")
        try:
            source_message_id = int(source_message_id_raw)
        except (TypeError, ValueError):
            await self._answer_callback(callback, "Queue item unavailable")
            return
        key = await self._resolve_topic_key(callback.chat_id, callback.thread_id)
        runtime = self._router.runtime_for(key)
        cancelled = runtime.queue.cancel_pending_item(str(source_message_id))
        if not cancelled:
            await self._answer_callback(callback, "Queue item is no longer pending")
            return
        placeholder_id = self._get_queued_placeholder(
            callback.chat_id, source_message_id
        )
        self._clear_queued_placeholder(callback.chat_id, source_message_id)
        if placeholder_id is not None:
            await self._delete_message(
                callback.chat_id,
                placeholder_id,
                callback.thread_id,
            )
        await self._answer_callback(callback, "Queued message cancelled")

    async def _handle_queue_interrupt_send_callback(
        self, callback: TelegramCallbackQuery, kind: str
    ) -> None:
        if callback.chat_id is None:
            await self._answer_callback(callback, "Queue item unavailable")
            return
        _prefix, _sep, source_message_id_raw = kind.partition(":")
        try:
            source_message_id = int(source_message_id_raw)
        except (TypeError, ValueError):
            await self._answer_callback(callback, "Queue item unavailable")
            return
        key = await self._resolve_topic_key(callback.chat_id, callback.thread_id)
        runtime = self._router.runtime_for(key)
        promoted = runtime.queue.promote_pending_item(str(source_message_id))
        if not promoted:
            await self._answer_callback(callback, "Queue item is no longer pending")
            return
        if runtime.current_turn_id is None:
            await self._answer_callback(callback, "Queued message moved to front")
            return
        await self._answer_callback(callback, "Interrupting current turn...")
        await self._process_interrupt(
            chat_id=callback.chat_id,
            thread_id=callback.thread_id,
            reply_to=callback.message_id,
            runtime=runtime,
            message_id=callback.message_id,
            preserve_queue=True,
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
        preserve_queue: bool = False,
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
        record = await self._router.get_topic(key)
        if (
            record is not None
            and getattr(self._config, "root", None) is not None
            and callable(getattr(self, "_spawn_task", None))
        ):
            from .commands.execution import (
                _get_telegram_thread_binding,
            )

            mode = "pma" if bool(getattr(record, "pma_enabled", False)) else "repo"
            orchestration_service, _binding, current_thread = (
                _get_telegram_thread_binding(
                    self,
                    surface_key=key,
                    mode=mode,
                )
            )
            if current_thread is not None:
                pma_mode = bool(getattr(record, "pma_enabled", False))
                operation_id_getter = getattr(self, "_current_chat_operation_id", None)
                operation_id = (
                    operation_id_getter() if callable(operation_id_getter) else None
                )
                interrupt_outcome = await request_managed_thread_interrupt(
                    orchestration_service=orchestration_service,
                    thread_target_id=current_thread.thread_target_id,
                    cancel_queued=True,
                    operation_store=getattr(self, "_chat_operation_store", None),
                    operation_id=operation_id,
                )
                if interrupt_outcome.state == SharedInterruptState.FAILED_TO_DISPATCH:
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "telegram.interrupt.orchestration_failed",
                        chat_id=chat_id,
                        thread_id=thread_id,
                        message_id=message_id,
                        managed_thread_id=current_thread.thread_target_id,
                        mode=mode,
                        interrupt_state=interrupt_outcome.state.value,
                        error=interrupt_outcome.error,
                    )
                    await self._send_message(
                        chat_id,
                        (
                            "Failed to interrupt PMA turn."
                            if pma_mode
                            else "Failed to interrupt active turn."
                        ),
                        thread_id=thread_id,
                        reply_to=reply_to,
                    )
                    return
                if interrupt_outcome.state in {
                    SharedInterruptState.CONFIRMED,
                    SharedInterruptState.STILL_STOPPING,
                    SharedInterruptState.ALREADY_FINISHED,
                }:
                    await self._send_message(
                        chat_id,
                        render_managed_thread_interrupt_message(
                            interrupt_outcome,
                            active_turn_text=(
                                "Interrupted active PMA turn."
                                if pma_mode
                                else "Interrupted active turn."
                            ),
                            still_stopping_text=(
                                "Still stopping active PMA turn..."
                                if pma_mode
                                else "Still stopping active turn..."
                            ),
                            already_finished_text=(
                                "Active PMA turn already finished."
                                if pma_mode
                                else "Active turn already finished."
                            ),
                            recovered_lost_backend_text=(
                                "Recovered stale PMA session after backend thread was lost."
                                if pma_mode
                                else "Recovered stale session after backend thread was lost."
                            ),
                            queued_text_template=(
                                "Cancelled {count} queued PMA turn(s)."
                                if pma_mode
                                else "Cancelled {count} queued turn(s)."
                            ),
                        ),
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
            not preserve_queue
            and runtime.queued_turn_cancel is not None
            and not runtime.queued_turn_cancel.is_set()
        ):
            runtime.queued_turn_cancel.set()
            queued_turn_cancelled = True
        queued_cancelled = 0 if preserve_queue else runtime.queue.cancel_pending()
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
        _interrupt_snapshot = _Snap(platform="telegram", channel_id=str(chat_id))
        _interrupt_snapshot.record(ChatUxMilestone.RAW_EVENT_RECEIVED)
        _interrupt_snapshot.record(ChatUxMilestone.INTERRUPT_REQUESTED_VISIBLE)
        log_event(
            self._logger,
            logging.INFO,
            "telegram.interrupt.acknowledged",
            chat_ux_platform="telegram",
            chat_id=chat_id,
            thread_id=thread_id,
            message_id=message_id,
            turn_id=turn_id,
            **_interrupt_snapshot.to_log_fields(),
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
        command_policy, plain_text_policy = evaluate_collaboration_summary(
            self,
            message,
            command_text="/debug",
        )
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
        lines.extend(
            collaboration_summary_lines(
                message,
                command_result=command_policy,
                plain_text_result=plain_text_policy,
            )
        )
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
            except OSError:
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
        command_policy, plain_text_policy = evaluate_collaboration_summary(
            self,
            message,
            command_text="/ids",
        )
        lines = [
            f"Chat ID: {message.chat_id}",
            f"Thread ID: {message.thread_id or 'none'}",
            f"User ID: {message.from_user_id or 'unknown'}",
            f"Topic key: {key}",
            "",
        ]
        lines.extend(
            collaboration_summary_lines(
                message,
                command_result=command_policy,
                plain_text_result=plain_text_policy,
            )
        )
        lines.extend(
            [
                "",
                "Legacy allowlist example:",
                f"telegram_bot.allowed_chat_ids: [{message.chat_id}]",
            ]
        )
        if message.from_user_id is not None:
            lines.append(f"telegram_bot.allowed_user_ids: [{message.from_user_id}]")
        lines.extend(
            [
                "",
                *build_collaboration_snippet_lines(message),
            ]
        )
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
        supports_model_listing = self._agent_supports_capability(agent, "model_listing")
        supports_manual_model_override = self._agent_supports_capability(
            agent, "message_turns"
        )
        agent_label = self._agent_display_name(agent)
        argv = self._parse_command_args(args)

        def _model_catalog_unavailable_text() -> str:
            if supports_manual_model_override:
                return (
                    f"{agent_label} does not expose a model catalog. "
                    "Use /model set <model> to override manually."
                )
            return f"{agent_label} does not support model selection in Telegram."

        list_params = {
            "cursor": None,
            "limit": DEFAULT_MODEL_LIST_LIMIT,
            "agent": agent,
        }
        workspace_path, error = self._resolve_workspace_path(record, allow_pma=True)
        if workspace_path is None:
            await self._send_message(
                message.chat_id,
                error or TOPIC_NOT_BOUND_MESSAGE,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if not supports_model_listing:
            if not argv or argv[0].lower() in ("list", "ls"):
                await self._send_message(
                    message.chat_id,
                    _model_catalog_unavailable_text(),
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
        client = None
        if supports_model_listing:
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
                    APP_SERVER_UNAVAILABLE_MESSAGE,
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            if client is None:
                await self._send_message(
                    message.chat_id,
                    error or TOPIC_NOT_BOUND_MESSAGE,
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
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
            except (
                RuntimeError,
                OSError,
                ValueError,
                TypeError,
                ConnectionError,
            ) as exc:
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
        search_query = argv[0].strip() if argv else None
        search_query_lower = search_query.lower() if search_query else None
        resolved_model_id: Optional[str] = None

        if (
            supports_model_listing
            and search_query
            and search_query_lower
            not in {
                "list",
                "ls",
                "clear",
                "reset",
                "set",
            }
        ):
            result: Any | None = None
            cached_result = self._model_catalog_cache.get(cache_key)
            if cached_result is not None:
                cached_payload, cached_time = cached_result
                if time.monotonic() - cached_time < MODEL_CATALOG_TTL_SECONDS:
                    result = cached_payload
            if result is None:
                record_for_models = self._record_with_workspace_path(
                    record, workspace_path
                )
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
                except (
                    RuntimeError,
                    OSError,
                    ValueError,
                    TypeError,
                    ConnectionError,
                ) as exc:
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "telegram.model.list.failed",
                        chat_id=message.chat_id,
                        thread_id=message.thread_id,
                        agent=agent,
                        exc=exc,
                    )
                    result = None

            if result is not None:
                options = _coerce_model_options(result, include_efforts=supports_effort)
                items = [(option.model_id, option.label) for option in options]
                exact = find_exact_picker_item(items, search_query)
                if exact is not None:
                    resolved_model_id = exact[0]
                else:
                    filtered_items = filter_picker_items(
                        items,
                        search_query,
                        limit=DEFAULT_MODEL_LIST_LIMIT,
                    )
                    if filtered_items:
                        filtered_ids = {model_id for model_id, _ in filtered_items}
                        filtered_options = [
                            option
                            for option in options
                            if option.model_id in filtered_ids
                        ]
                        state = ModelPickerState(
                            items=filtered_items,
                            options={
                                option.model_id: option for option in filtered_options
                            },
                            requester_user_id=(
                                str(message.from_user_id)
                                if message.from_user_id is not None
                                else None
                            ),
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
                            f"Search: '{search_query}' (showing {len(filtered_items)} results)",
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
            except (
                RuntimeError,
                OSError,
                ValueError,
                TypeError,
                ConnectionError,
            ) as exc:
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
                requester_user_id=(
                    str(message.from_user_id)
                    if message.from_user_id is not None
                    else None
                ),
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
        if argv[0].lower() == "set" and len(argv) < 2:
            await self._send_message(
                message.chat_id,
                "Usage: /model set <model> [effort]",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if argv[0].lower() == "set" and len(argv) > 1:
            model = argv[1]
            effort = argv[2] if len(argv) > 2 else None
        else:
            model = resolved_model_id or argv[0]
            effort = argv[1] if len(argv) > 1 else None
        if effort and not supports_effort:
            await self._send_message(
                message.chat_id,
                f"Reasoning effort is not supported for {agent}.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if agent == "opencode" and "/" not in model:
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
                f"Unknown effort '{effort}'. Allowed: {', '.join(REASONING_EFFORT_VALUES)}.",
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

    def _load_hub_pma_config(self) -> Any:
        hub_config = self._load_hub_config_from_file()
        return hub_config.pma if hub_config is not None else None

    def _load_hub_config_from_file(self) -> Any:
        hub_config_path = getattr(self, "_hub_config_path", None)
        if hub_config_path and Path(hub_config_path).exists():
            try:
                return load_hub_config(Path(hub_config_path).parent.parent)
            except (OSError, ValueError):
                pass
        return None

    async def _handle_pma(
        self, message: TelegramMessage, args: str, _runtime: Any
    ) -> None:
        def status_text(record: Optional[TelegramTopicRecord]) -> str:
            if record is None:
                return "PMA mode: disabled\nCurrent workspace: unbound"
            if record.pma_enabled:
                return "PMA mode: enabled"
            workspace = record.workspace_path or "unbound"
            return f"PMA mode: disabled\nCurrent workspace: {workspace}"

        if not self._hub_root:
            await self._send_message(
                message.chat_id,
                "PMA unavailable; hub root not configured.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        supervisor = getattr(self, "_hub_supervisor", None)
        if supervisor is not None and hasattr(supervisor, "hub_config"):
            pma_config = supervisor.hub_config.pma
        else:
            pma_config = self._load_hub_pma_config()
        if pma_config is not None and not pma_config.enabled:
            await self._send_message(
                message.chat_id,
                "PMA is disabled in hub config. Set pma.enabled: true to enable.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        argv = self._parse_command_args(args)
        action = argv[0].lower() if argv else ""

        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        record = await self._router.get_topic(key)
        current = bool(record and record.pma_enabled)
        if action in ("on", "enable", "true"):
            enabled = True
        elif action in ("off", "disable", "false"):
            enabled = False
        elif action in ("status", "show", ""):
            text = status_text(record)
            if record is not None and record.pma_enabled and self._hub_root is not None:
                process_lines = build_process_monitor_lines_for_root(
                    self._hub_root,
                    include_history=False,
                )
                if process_lines:
                    text = "\n".join([text, *process_lines])
            await self._send_message(
                message.chat_id,
                text,
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

        if enabled and current:
            await self._send_message(
                message.chat_id,
                "PMA mode is already enabled for this topic. Use /pma off to exit.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        if not enabled and record is None:
            await self._send_message(
                message.chat_id,
                "PMA mode disabled. Back to repo mode.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        previous_binding_workspace = record.workspace_path if record else None
        restored_workspace = record.pma_prev_workspace_path if record else None

        if record is None:
            record = await self._router.ensure_topic(message.chat_id, message.thread_id)

        def apply_pma(record: TelegramTopicRecord) -> None:
            record.pma_enabled = enabled
            if enabled:
                # Save previous binding before entering PMA mode.
                record.pma_prev_repo_id = record.repo_id
                record.pma_prev_resource_kind = record.resource_kind
                record.pma_prev_resource_id = record.resource_id
                record.pma_prev_workspace_path = record.workspace_path
                record.pma_prev_workspace_id = record.workspace_id
                record.pma_prev_active_thread_id = record.active_thread_id
                # Mutual exclusion: PMA mode implies Hub context, so unbind specific repo.
                record.workspace_path = None
                record.repo_id = None
                record.resource_kind = None
                record.resource_id = None
                record.workspace_id = None
                record.active_thread_id = None
            else:
                # Restore previous binding when exiting PMA mode.
                if record.pma_prev_repo_id or record.pma_prev_workspace_path:
                    record.repo_id = record.pma_prev_repo_id
                    record.resource_kind = record.pma_prev_resource_kind
                    record.resource_id = record.pma_prev_resource_id
                    record.workspace_path = record.pma_prev_workspace_path
                    record.workspace_id = record.pma_prev_workspace_id
                    record.active_thread_id = record.pma_prev_active_thread_id
                    # Clear saved previous binding after restore.
                    record.pma_prev_repo_id = None
                    record.pma_prev_resource_kind = None
                    record.pma_prev_resource_id = None
                    record.pma_prev_workspace_path = None
                    record.pma_prev_workspace_id = None
                    record.pma_prev_active_thread_id = None

        await self._router.update_topic(
            message.chat_id,
            message.thread_id,
            apply_pma,
        )

        if enabled:
            text = (
                "PMA mode enabled. Use /pma off to exit. Previous binding saved."
                if previous_binding_workspace
                else "PMA mode enabled. Use /pma off to exit."
            )
        else:
            if restored_workspace:
                text = f"PMA mode disabled. Restored binding to {restored_workspace}."
            else:
                text = "PMA mode disabled. Back to repo mode."
        await self._send_message(
            message.chat_id,
            text,
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    def _pma_usage(self) -> str:
        return "\n".join(
            [
                "Usage:",
                "/pma [on|off|status]",
            ]
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
                TOPIC_NOT_BOUND_MESSAGE,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        try:
            result = await client.request(
                "skills/list",
                {"cwds": [record.workspace_path], "forceReload": False},
            )
        except (RuntimeError, OSError, ValueError, TypeError, ConnectionError) as exc:
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
        except (RuntimeError, OSError, ValueError, TypeError, ConnectionError) as exc:
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
                TOPIC_NOT_BOUND_MESSAGE,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        try:
            result = await client.request(
                "mcpServerStatus/list",
                {"cursor": None, "limit": DEFAULT_MCP_LIST_LIMIT},
            )
        except (
            RuntimeError,
            OSError,
            ValueError,
            TypeError,
            ConnectionError,
            CodexAppServerError,
        ) as exc:
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
                TOPIC_NOT_BOUND_MESSAGE,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        argv = self._parse_command_args(args)

        async def _read_explicit_config_features() -> Optional[str]:
            try:
                result = await client.request("config/read", {"includeLayers": False})
            except (RuntimeError, OSError, ValueError, TypeError, ConnectionError):
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
            except (
                RuntimeError,
                OSError,
                ValueError,
                TypeError,
                ConnectionError,
            ) as exc:
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
        except (RuntimeError, OSError, ValueError, TypeError, ConnectionError) as exc:
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
                error or TOPIC_NOT_BOUND_MESSAGE,
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
        has_managed_thread_runtime = getattr(
            self._config, "root", None
        ) is not None and callable(getattr(self, "_spawn_task", None))
        managed_thread_mode = (
            "pma" if bool(getattr(record, "pma_enabled", False)) else "repo"
        )
        if not has_managed_thread_runtime:
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
                return False, APP_SERVER_UNAVAILABLE_MESSAGE
            if client is None:
                return (
                    False,
                    error or TOPIC_NOT_BOUND_MESSAGE,
                )
            try:
                thread = await client.thread_start(
                    workspace_path,
                    **self._thread_start_kwargs(record),
                )
            except (
                RuntimeError,
                OSError,
                ValueError,
                TypeError,
                ConnectionError,
            ) as exc:
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
            key = await self._resolve_topic_key(message.chat_id, message.thread_id)
            try:
                from ...chat.managed_thread_lifecycle import (
                    bind_surface_thread,
                    replace_surface_thread,
                )
                from .commands.execution import _get_telegram_thread_binding

                orchestration_service, binding, current_thread = (
                    _get_telegram_thread_binding(
                        self,
                        surface_key=key,
                        mode=managed_thread_mode,
                    )
                )
                agent = self._effective_runtime_agent(record)
                agent_profile = self._effective_agent_profile(record)
                replacement = await replace_surface_thread(
                    orchestration_service,
                    surface_kind="telegram",
                    surface_key=key,
                    workspace_root=Path(workspace_path),
                    agent_id=agent,
                    repo_id=(
                        record.repo_id.strip()
                        if isinstance(record.repo_id, str) and record.repo_id.strip()
                        else None
                    ),
                    resource_kind=(
                        record.resource_kind.strip()
                        if isinstance(record.resource_kind, str)
                        and record.resource_kind.strip()
                        else None
                    ),
                    resource_id=(
                        record.resource_id.strip()
                        if isinstance(record.resource_id, str)
                        and record.resource_id.strip()
                        else None
                    ),
                    mode=managed_thread_mode,
                    display_name=f"telegram:{key}",
                    binding_metadata={
                        "topic_key": key,
                        "pma_enabled": bool(getattr(record, "pma_enabled", False)),
                        "surface_key": key,
                    },
                    thread_metadata=(
                        {"agent_profile": agent_profile} if agent_profile else None
                    ),
                    binding=binding,
                    thread=current_thread,
                )
                replacement_thread = bind_surface_thread(
                    orchestration_service,
                    surface_kind="telegram",
                    surface_key=key,
                    thread_target_id=replacement.replacement_thread.thread_target_id,
                    agent_id=agent,
                    repo_id=(
                        record.repo_id.strip()
                        if isinstance(record.repo_id, str) and record.repo_id.strip()
                        else None
                    ),
                    resource_kind=(
                        record.resource_kind.strip()
                        if isinstance(record.resource_kind, str)
                        and record.resource_kind.strip()
                        else None
                    ),
                    resource_id=(
                        record.resource_id.strip()
                        if isinstance(record.resource_id, str)
                        and record.resource_id.strip()
                        else None
                    ),
                    mode=managed_thread_mode,
                    metadata={
                        "topic_key": key,
                        "pma_enabled": bool(getattr(record, "pma_enabled", False)),
                        "surface_key": key,
                    },
                    backend_thread_id=new_thread_id,
                    thread=replacement.replacement_thread,
                )
                hub_client = getattr(self, "_hub_client", None)
                if hub_client is not None:
                    from ....core.hub_control_plane import (
                        ThreadCompactSeedUpdateRequest as _CPCompactSeedRequest,
                    )

                    with contextlib.suppress(Exception):
                        await hub_client.update_thread_compact_seed(
                            _CPCompactSeedRequest(
                                thread_target_id=replacement_thread.thread_target_id,
                                compact_seed=summary_text,
                            )
                        )
                else:
                    log_event(
                        self._logger,
                        logging.WARNING,
                        "telegram.compact.seed_save.hub_client_unavailable",
                        chat_id=message.chat_id,
                        thread_id=message.thread_id,
                    )
            except (
                RuntimeError,
                OSError,
                ValueError,
                TypeError,
                ConnectionError,
            ) as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.compact.lifecycle_failed",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    codex_thread_id=new_thread_id,
                    exc=exc,
                )
                return False, "Failed to prepare a fresh managed thread."
            record = await self._apply_thread_result(
                message.chat_id,
                message.thread_id,
                thread,
                active_thread_id=new_thread_id,
                sync_binding=False,
            )
            seed_text = self._build_compact_seed_prompt(summary_text)
            record = await self._router.update_topic(
                message.chat_id,
                message.thread_id,
                lambda record: _set_pending_compact_seed(
                    record, seed_text, new_thread_id
                ),
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
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        try:
            from ...chat.managed_thread_lifecycle import replace_surface_thread
            from .commands.execution import _get_telegram_thread_binding

            orchestration_service, binding, current_thread = (
                _get_telegram_thread_binding(
                    self,
                    surface_key=key,
                    mode=managed_thread_mode,
                )
            )
            agent = self._effective_runtime_agent(record)
            agent_profile = self._effective_agent_profile(record)
            replacement = await replace_surface_thread(
                orchestration_service,
                surface_kind="telegram",
                surface_key=key,
                workspace_root=Path(workspace_path),
                agent_id=agent,
                repo_id=(
                    record.repo_id.strip()
                    if isinstance(record.repo_id, str) and record.repo_id.strip()
                    else None
                ),
                resource_kind=(
                    record.resource_kind.strip()
                    if isinstance(record.resource_kind, str)
                    and record.resource_kind.strip()
                    else None
                ),
                resource_id=(
                    record.resource_id.strip()
                    if isinstance(record.resource_id, str)
                    and record.resource_id.strip()
                    else None
                ),
                mode=managed_thread_mode,
                display_name=f"telegram:{key}",
                binding_metadata={
                    "topic_key": key,
                    "pma_enabled": bool(getattr(record, "pma_enabled", False)),
                    "surface_key": key,
                },
                thread_metadata=(
                    {"agent_profile": agent_profile} if agent_profile else None
                ),
                binding=binding,
                thread=current_thread,
            )
            replacement_thread = replacement.replacement_thread
            hub_client = getattr(self, "_hub_client", None)
            if hub_client is not None:
                from ....core.hub_control_plane import (
                    ThreadCompactSeedUpdateRequest as _CPCompactSeedRequest,
                )

                with contextlib.suppress(Exception):
                    await hub_client.update_thread_compact_seed(
                        _CPCompactSeedRequest(
                            thread_target_id=replacement_thread.thread_target_id,
                            compact_seed=summary_text,
                        )
                    )
            else:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.compact.seed_save.hub_client_unavailable",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                )
        except (RuntimeError, OSError, ValueError, TypeError, ConnectionError) as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.compact.lifecycle_failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                managed_thread_id=(
                    replacement_thread.thread_target_id
                    if "replacement_thread" in locals()
                    else None
                ),
                exc=exc,
            )
            return False, "Failed to prepare a fresh managed thread."
        seed_text = self._build_compact_seed_prompt(summary_text)

        def _apply_compact_state(updated: "TelegramTopicRecord") -> None:
            updated.active_thread_id = None
            _set_pending_compact_seed(
                updated,
                seed_text,
                replacement_thread.thread_target_id,
            )

        record = await self._router.update_topic(
            message.chat_id,
            message.thread_id,
            _apply_compact_state,
        )
        log_event(
            self._logger,
            logging.INFO,
            "telegram.compact.apply.seed_queued",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            managed_thread_id=replacement_thread.thread_target_id,
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
        summary_message_id, _display_text = await self._send_compact_summary_message(
            message, summary_text, reply_markup=None
        )
        interrupt_status_turn_id = getattr(outcome, "interrupt_status_turn_id", None)
        interrupt_status_fallback_text = getattr(
            outcome, "interrupt_status_fallback_text", None
        )
        if interrupt_status_fallback_text and interrupt_status_turn_id:
            await self._clear_interrupt_status_message(
                chat_id=message.chat_id,
                runtime=runtime,
                turn_id=interrupt_status_turn_id,
                fallback_text=interrupt_status_fallback_text,
                outcome_visible=summary_message_id is not None,
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
            except (RuntimeError, OSError, TypeError, ValueError):
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
        status = await asyncio.to_thread(
            self._write_compact_status,
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
            status = await asyncio.to_thread(
                self._write_compact_status,
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
        status = await asyncio.to_thread(
            self._write_compact_status,
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
                TOPIC_NOT_BOUND_MESSAGE,
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
        except (RuntimeError, OSError, ValueError, TypeError, ConnectionError) as exc:
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
            except (
                RuntimeError,
                OSError,
                ValueError,
                TypeError,
                ConnectionError,
            ) as exc:
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
        )
        callback_answered = False
        if callback is not None:
            await self._answer_callback(callback, "Starting update...")
            callback_answered = True
        try:
            await asyncio.to_thread(
                _spawn_update_process,
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
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
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
                if not callback_answered:
                    await self._answer_callback(callback, "Update failed")
                await self._finalize_selection(selection_key, callback, failure)
            else:
                await self._send_message(
                    chat_id, failure, thread_id=thread_id, reply_to=reply_to
                )
            return
        target_label = get_update_target_label(update_target)
        message = format_update_started_message(
            target_label,
            status_command="/update status",
            completion_scope_label="this thread",
        )
        if callback and selection_key:
            if not callback_answered:
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
        state = SelectionState(
            items=list(self._dynamic_update_target_options()),
            requester_user_id=(
                str(message.from_user_id) if message.from_user_id is not None else None
            ),
        )
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
        state = SelectionState(
            items=list(self._dynamic_update_target_options()),
            requester_user_id=(
                str(callback.from_user_id)
                if callback.from_user_id is not None
                else None
            ),
        )
        keyboard = self._build_update_keyboard(state)
        self._update_options[key] = state
        self._touch_cache_timestamp("update_options", key)
        await self._update_selection_message(key, callback, prompt, keyboard)

    def _dynamic_update_target_options(self) -> tuple[tuple[str, str], ...]:
        update_backend = str(getattr(self, "_update_backend", "auto") or "auto")
        update_services = getattr(self, "_update_linux_service_names", None)
        raw_config: Optional[dict[str, Any]] = None

        supervisor = getattr(self, "_hub_supervisor", None)
        if supervisor is not None and hasattr(supervisor, "hub_config"):
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
        else:
            hub_config = self._load_hub_config_from_file()
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

    def _active_update_session_count(self) -> int:
        return len(self._turn_contexts)

    async def _prompt_update_confirmation(
        self,
        message: TelegramMessage,
        *,
        update_target: Optional[str] = None,
    ) -> None:
        key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        self._update_confirm_options[key] = UpdateConfirmState(target=update_target)
        self._touch_cache_timestamp("update_confirm_options", key)
        prompt = (
            _format_update_confirmation_warning(
                active_count=self._active_update_session_count(),
                singular_label="Codex session",
            )
            or "Updating will restart the service. Continue?"
        )
        await self._send_message(
            message.chat_id,
            prompt,
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
        except (OSError, json.JSONDecodeError, ValueError):
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
        notify_context = TelegramNoticeContext(
            chat_id=chat_id,
            thread_id=thread_id,
            reply_to=reply_to,
        )
        self._update_status_notifier.schedule_watch(
            notify_context.to_payload(),
            timeout_seconds=timeout_seconds,
            interval_seconds=interval_seconds,
        )

    async def _send_update_status_notice(
        self, notify_context: dict[str, Any], text: str
    ) -> None:
        context = TelegramNoticeContext.from_payload(notify_context)
        if context is None:
            return
        await self._send_message(
            context.chat_id,
            text,
            thread_id=context.thread_id,
            reply_to=context.reply_to,
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

    def _read_compact_status(self) -> Optional[CompactStatusState]:
        path = self._compact_status_path()
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return None
        return CompactStatusState.from_payload(data)

    def _write_compact_status(
        self, status: str, message: str, **extra: Any
    ) -> CompactStatusState:
        payload = CompactStatusState(
            status=status,
            message=message,
            at=time.time(),
            chat_id=(
                extra["chat_id"]
                if isinstance(extra.get("chat_id"), int)
                and not isinstance(extra.get("chat_id"), bool)
                else None
            ),
            thread_id=(
                extra["thread_id"]
                if isinstance(extra.get("thread_id"), int)
                and not isinstance(extra.get("thread_id"), bool)
                else None
            ),
            message_id=(
                extra["message_id"]
                if isinstance(extra.get("message_id"), int)
                and not isinstance(extra.get("message_id"), bool)
                else None
            ),
            display_text=(
                extra["display_text"]
                if isinstance(extra.get("display_text"), str)
                else None
            ),
            error_detail=(
                extra["error_detail"]
                if isinstance(extra.get("error_detail"), str)
                else None
            ),
            started_at=(
                float(extra["started_at"])
                if isinstance(extra.get("started_at"), (int, float))
                and not isinstance(extra.get("started_at"), bool)
                else None
            ),
            notify_sent_at=(
                float(extra["notify_sent_at"])
                if isinstance(extra.get("notify_sent_at"), (int, float))
                and not isinstance(extra.get("notify_sent_at"), bool)
                else None
            ),
        )
        path = self._compact_status_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload.to_payload()), encoding="utf-8")
        except (OSError, TypeError) as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.compact.status_write_failed",
                exc=exc,
            )
        return payload

    def _mark_compact_notified(self, status: CompactStatusState) -> None:
        path = self._compact_status_path()
        updated = status.with_notify_sent_at(time.time())
        try:
            path.write_text(json.dumps(updated.to_payload()), encoding="utf-8")
        except (OSError, TypeError) as exc:
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
        if not status or status.notify_sent_at is not None:
            return
        chat_id = status.chat_id
        if chat_id is None:
            return
        thread_id = status.thread_id
        message_id = status.message_id
        display_text = status.display_text
        state = status.status
        message = status.message
        if state == "running":
            message = "Compact apply interrupted by restart. Please retry."
            status = await asyncio.to_thread(
                self._write_compact_status,
                "interrupted",
                message,
                chat_id=chat_id,
                thread_id=thread_id,
                message_id=message_id,
                display_text=display_text,
                started_at=status.at,
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
            except (RuntimeError, OSError, TypeError, ValueError):
                try:
                    await self._send_message(chat_id, message, thread_id=thread_id)
                    sent = True
                except (RuntimeError, OSError, TypeError, ValueError):
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
        if self._has_active_turns() and _update_target_restarts_surface(
            update_target, surface="telegram"
        ):
            await self._prompt_update_confirmation(
                message,
                update_target=update_target,
            )
            return
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
                error or TOPIC_NOT_BOUND_MESSAGE,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        client = await self._client_for_workspace(workspace_path)
        if client is None:
            await self._send_message(
                message.chat_id,
                error or TOPIC_NOT_BOUND_MESSAGE,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        try:
            await client.request("account/logout", params=None)
        except (RuntimeError, OSError, ValueError, TypeError, ConnectionError) as exc:
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
                error or TOPIC_NOT_BOUND_MESSAGE,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        client = await self._client_for_workspace(workspace_path)
        if client is None:
            await self._send_message(
                message.chat_id,
                error or TOPIC_NOT_BOUND_MESSAGE,
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
        except (RuntimeError, OSError, ValueError, TypeError, ConnectionError) as exc:
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
