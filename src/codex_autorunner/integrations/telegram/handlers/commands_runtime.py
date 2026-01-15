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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Sequence

from ....core.config import load_config
from ....core.injected_context import wrap_injected_context
from ....core.logging_utils import log_event
from ....core.state import now_iso
from ....core.update import _normalize_update_target, _spawn_update_process
from ....core.utils import canonicalize_path
from ....integrations.github.service import GitHubService
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
    TelegramCallbackQuery,
    TelegramCommand,
    TelegramMessage,
    build_compact_keyboard,
    build_inline_keyboard,
    build_update_confirm_keyboard,
    encode_cancel_callback,
)
from ..config import TelegramMediaCandidate
from ..constants import (
    APPROVAL_POLICY_VALUES,
    APPROVAL_PRESETS,
    BIND_PICKER_PROMPT,
    COMMAND_DISABLED_TEMPLATE,
    COMPACT_SUMMARY_PROMPT,
    DEFAULT_MCP_LIST_LIMIT,
    DEFAULT_MODEL_LIST_LIMIT,
    DEFAULT_PAGE_SIZE,
    DEFAULT_UPDATE_REPO_REF,
    DEFAULT_UPDATE_REPO_URL,
    INIT_PROMPT,
    MAX_MENTION_BYTES,
    MAX_TOPIC_THREAD_HISTORY,
    MODEL_PICKER_PROMPT,
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
from ..state import APPROVAL_MODE_YOLO, PendingVoiceRecord, parse_topic_key, topic_key
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
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
        spec = self._command_specs.get(name)
        if spec is None:
            self._resume_options.pop(key, None)
            self._bind_options.pop(key, None)
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

    async def _verify_active_thread(
        self, message: TelegramMessage, record: "TelegramTopicRecord"
    ) -> Optional["TelegramTopicRecord"]:
        thread_id = record.active_thread_id
        if not thread_id:
            return record
        client = await self._client_for_workspace(record.workspace_path)
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
            return self._router.set_active_thread(
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
            return self._router.set_active_thread(
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
            return self._router.set_active_thread(
                message.chat_id, message.thread_id, None
            )
        return self._apply_thread_result(
            message.chat_id, message.thread_id, result, active_thread_id=thread_id
        )

    def _find_thread_conflict(self, thread_id: str, *, key: str) -> Optional[str]:
        return self._store.find_active_thread(thread_id, exclude_key=key)

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

    def _apply_thread_result(
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

        return self._router.update_topic(chat_id, thread_id, apply)

    async def _require_bound_record(
        self, message: TelegramMessage, *, prompt: Optional[str] = None
    ) -> Optional["TelegramTopicRecord"]:
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
        record = self._router.get_topic(key)
        if record is None or not record.workspace_path:
            await self._send_message(
                message.chat_id,
                prompt or "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        self._refresh_workspace_id(key, record)
        return record

    async def _ensure_thread_id(
        self, message: TelegramMessage, record: "TelegramTopicRecord"
    ) -> Optional[str]:
        thread_id = record.active_thread_id
        if thread_id:
            key = self._resolve_topic_key(message.chat_id, message.thread_id)
            conflict_key = self._find_thread_conflict(thread_id, key=key)
            if conflict_key:
                self._router.set_active_thread(message.chat_id, message.thread_id, None)
                await self._handle_thread_conflict(message, thread_id, conflict_key)
                return None
            verified = await self._verify_active_thread(message, record)
            if not verified:
                return None
            record = verified
            thread_id = record.active_thread_id
            if thread_id:
                return thread_id
        client = await self._client_for_workspace(record.workspace_path)
        if client is None:
            await self._send_message(
                message.chat_id,
                "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        thread = await client.thread_start(record.workspace_path or "")
        if not await self._require_thread_workspace(
            message, record.workspace_path, thread, action="thread_start"
        ):
            return None
        thread_id = _extract_thread_id(thread)
        if not thread_id:
            await self._send_message(
                message.chat_id,
                "Failed to start a new Codex thread.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        self._apply_thread_result(
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
    ) -> None:
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
        )
        if isinstance(outcome, _TurnRunFailure):
            return
        response_sent = await self._deliver_turn_response(
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            reply_to=message.message_id,
            placeholder_id=outcome.placeholder_id,
            response=outcome.response,
        )
        await self._send_turn_metrics(
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            reply_to=message.message_id,
            elapsed_seconds=outcome.elapsed_seconds,
            token_usage=outcome.token_usage,
        )
        if outcome.turn_id:
            self._token_usage_by_turn.pop(outcome.turn_id, None)
        if response_sent:
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
    ) -> _TurnRunResult | _TurnRunFailure:
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
        record = record or self._router.get_topic(key)
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
        client = await self._client_for_workspace(record.workspace_path)
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
        if record.active_thread_id:
            conflict_key = self._find_thread_conflict(
                record.active_thread_id,
                key=key,
            )
            if conflict_key:
                self._router.set_active_thread(message.chat_id, message.thread_id, None)
                await self._handle_thread_conflict(
                    message,
                    record.active_thread_id,
                    conflict_key,
                )
                return _TurnRunFailure(
                    "Thread conflict detected.",
                    None,
                    transcript_message_id,
                    transcript_text,
                )
            verified = await self._verify_active_thread(message, record)
            if not verified:
                return _TurnRunFailure(
                    "Active thread verification failed.",
                    None,
                    transcript_message_id,
                    transcript_text,
                )
            record = verified
        thread_id = record.active_thread_id
        turn_handle = None
        turn_key: Optional[TurnKey] = None
        placeholder_id: Optional[int] = None
        turn_started_at: Optional[float] = None
        turn_elapsed_seconds: Optional[float] = None
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
                thread = await client.thread_start(workspace_path)
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
                    failure_message = "Failed to start a new Codex thread."
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
                record = self._apply_thread_result(
                    message.chat_id,
                    message.thread_id,
                    thread,
                    active_thread_id=thread_id,
                )
            else:
                record = self._router.set_active_thread(
                    message.chat_id, message.thread_id, thread_id
                )
            if thread_id:
                user_preview = _preview_from_text(
                    prompt_text, RESUME_PREVIEW_USER_LIMIT
                )
                self._router.update_topic(
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
            turn_kwargs: dict[str, Any] = {}
            if record.model:
                turn_kwargs["model"] = record.model
            if record.effort:
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
                approval_mode=record.approval_mode,
                approval_policy=approval_policy,
                sandbox_policy=sandbox_policy,
            )

            turn_semaphore = self._ensure_turn_semaphore()
            queued = turn_semaphore.locked()
            placeholder_text = PLACEHOLDER_TEXT
            if queued:
                placeholder_text = QUEUED_PLACEHOLDER_TEXT
            if send_placeholder:
                placeholder_id = await self._send_placeholder(
                    message.chat_id,
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                    text=placeholder_text,
                )
            queue_started_at = time.monotonic()
            async with turn_semaphore:
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
                    self._router.update_topic(
                        message.chat_id,
                        message.thread_id,
                        _clear_pending_compact_seed,
                    )
                turn_started_at = time.monotonic()
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
                result = await turn_handle.wait()
                if turn_started_at is not None:
                    turn_elapsed_seconds = time.monotonic() - turn_started_at
        except Exception as exc:
            if turn_handle is not None:
                if turn_key is not None:
                    self._turn_contexts.pop(turn_key, None)
            runtime.current_turn_id = None
            runtime.current_turn_key = None
            runtime.interrupt_requested = False
            failure_message = "Codex turn failed; check logs for details."
            if isinstance(exc, CodexAppServerDisconnected):
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
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.turn.failed",
                topic_key=key,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
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
                self._router.update_topic(
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
        if token_usage is None and thread_id:
            token_usage = self._token_usage_by_thread.get(thread_id)
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
            repo_config = load_config(repo_root)
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
                candidate.file_id
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.media.image.download_failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                message_id=message.message_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                "Failed to download image.",
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
        )

    async def _handle_voice_message(
        self,
        message: TelegramMessage,
        runtime: Any,
        record: Any,
        candidate: TelegramMediaCandidate,
        caption_text: str,
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
        self._store.enqueue_pending_voice(pending)
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
                candidate.file_id
            )
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.media.file.download_failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                message_id=message.message_id,
                exc=exc,
            )
            await self._send_message(
                message.chat_id,
                "Failed to download file.",
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
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
        try:
            file_path_local = self._save_inbox_file(
                record.workspace_path,
                key,
                data,
                candidate=candidate,
                file_path=file_path,
            )
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
        )

    async def _download_telegram_file(
        self, file_id: str
    ) -> tuple[bytes, Optional[str], Optional[int]]:
        payload = await self._bot.get_file(file_id)
        file_path = payload.get("file_path") if isinstance(payload, dict) else None
        file_size = payload.get("file_size") if isinstance(payload, dict) else None
        if file_size is not None and not isinstance(file_size, int):
            file_size = None
        if not isinstance(file_path, str) or not file_path:
            raise RuntimeError("Telegram getFile returned no file_path")
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
            self._store.update_pending_voice(record)
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
        key = self._resolve_topic_key(record.chat_id, record.thread_id)
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
        key = topic_key or self._resolve_topic_key(chat_id, thread_id)
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
        turn_id = runtime.current_turn_id
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
        if (
            turn_id
            and runtime.interrupt_requested
            and runtime.interrupt_turn_id == turn_id
        ):
            await self._send_message(
                message.chat_id,
                "Already stopping current turn.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        pending_request_ids = [
            request_id
            for request_id, pending in self._pending_approvals.items()
            if (pending.topic_key == key)
            or (
                pending.topic_key is None
                and pending.chat_id == message.chat_id
                and pending.thread_id == message.thread_id
            )
        ]
        for request_id in pending_request_ids:
            pending = self._pending_approvals.pop(request_id, None)
            if pending and not pending.future.done():
                pending.future.set_result("cancel")
            self._store.clear_pending_approval(request_id)
        if pending_request_ids:
            runtime.pending_request_id = None
        if not turn_id:
            pending_records = self._store.pending_approvals_for_key(key)
            if pending_records:
                self._store.clear_pending_approvals_for_key(key)
                runtime.pending_request_id = None
                await self._send_message(
                    message.chat_id,
                    f"Cleared {len(pending_records)} pending approval(s).",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return
            log_event(
                self._logger,
                logging.INFO,
                "telegram.interrupt.none",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                message_id=message.message_id,
            )
            await self._send_message(
                message.chat_id,
                "No active turn to interrupt.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        runtime.interrupt_requested = True
        log_event(
            self._logger,
            logging.INFO,
            "telegram.interrupt.requested",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            message_id=message.message_id,
            turn_id=turn_id,
        )
        payload_text, parse_mode = self._prepare_outgoing_text(
            "Stopping current turn...",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
        response = await self._bot.send_message(
            message.chat_id,
            payload_text,
            message_thread_id=message.thread_id,
            reply_to_message_id=message.message_id,
            parse_mode=parse_mode,
        )
        message_id = response.get("message_id") if isinstance(response, dict) else None
        codex_thread_id = None
        if runtime.current_turn_key and runtime.current_turn_key[1] == turn_id:
            codex_thread_id = runtime.current_turn_key[0]
        if isinstance(message_id, int):
            runtime.interrupt_message_id = message_id
            runtime.interrupt_turn_id = turn_id
            self._spawn_task(
                self._interrupt_timeout_check(
                    self._resolve_topic_key(message.chat_id, message.thread_id),
                    turn_id,
                    message_id,
                )
            )
        self._spawn_task(
            self._dispatch_interrupt_request(
                turn_id=turn_id,
                codex_thread_id=codex_thread_id,
                runtime=runtime,
                chat_id=message.chat_id,
                thread_id=message.thread_id,
            )
        )

    async def _handle_bind(self, message: TelegramMessage, args: str) -> None:
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
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
        self._router.set_topic_scope(chat_id, thread_id, scope)
        self._router.bind_topic(
            chat_id,
            thread_id,
            workspace_path,
            repo_id=resolved_repo_id,
            scope=scope,
        )
        workspace_id = self._workspace_id_for_path(workspace_path)
        if workspace_id:
            self._router.update_topic(
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
        self._router.set_topic_scope(message.chat_id, message.thread_id, scope)
        self._router.bind_topic(
            message.chat_id,
            message.thread_id,
            workspace_path,
            repo_id=repo_id,
            scope=scope,
        )
        workspace_id = self._workspace_id_for_path(workspace_path)
        if workspace_id:
            self._router.update_topic(
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
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
        record = self._router.get_topic(key)
        if record is None or not record.workspace_path:
            await self._send_message(
                message.chat_id,
                "Topic not bound. Use /bind <repo_id> or /bind <path>.",
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
        thread = await client.thread_start(record.workspace_path)
        if not await self._require_thread_workspace(
            message, record.workspace_path, thread, action="thread_start"
        ):
            return
        thread_id = _extract_thread_id(thread)
        if not thread_id:
            await self._send_message(
                message.chat_id,
                "Failed to start a new Codex thread.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        self._apply_thread_result(
            message.chat_id, message.thread_id, thread, active_thread_id=thread_id
        )
        await self._send_message(
            message.chat_id,
            "\n".join(
                [
                    f"Started new thread {thread_id}.",
                    f"Directory: {record.workspace_path or 'unbound'}",
                    f"Model: {record.model or 'default'}",
                    f"Effort: {record.effort or 'default'}",
                ]
            ),
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_resume(self, message: TelegramMessage, args: str) -> None:
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
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
        record = self._router.get_topic(key)
        if record is None or not record.workspace_path:
            await self._send_message(
                message.chat_id,
                "Topic not bound. Use /bind <repo_id> or /bind <path>.",
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
            store_state = self._store.load()
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
                    store_state = self._store.load()
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
                    record = self._router.get_topic(key) or record
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
                    self._store.update_topic(key, apply)
            elif default_topic_key:
                self._store.update_topic(default_topic_key, apply)
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
        record = self._router.get_topic(key)
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
        client = await self._client_for_workspace(record.workspace_path)
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
        conflict_key = self._find_thread_conflict(thread_id, key=key)
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
        updated_record = self._apply_thread_result(
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

    async def _handle_status(
        self, message: TelegramMessage, _args: str = "", runtime: Optional[Any] = None
    ) -> None:
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
        record = self._router.ensure_topic(message.chat_id, message.thread_id)
        self._refresh_workspace_id(key, record)
        if runtime is None:
            runtime = self._router.runtime_for(key)
        approval_policy, sandbox_policy = self._effective_policies(record)
        lines = [
            f"Workspace: {record.workspace_path or 'unbound'}",
            f"Workspace ID: {record.workspace_id or 'unknown'}",
            f"Active thread: {record.active_thread_id or 'none'}",
            f"Active turn: {runtime.current_turn_id or 'none'}",
            f"Model: {record.model or 'default'}",
            f"Effort: {record.effort or 'default'}",
            f"Approval mode: {record.approval_mode}",
            f"Approval policy: {approval_policy or 'default'}",
            f"Sandbox policy: {_format_sandbox_policy(sandbox_policy)}",
        ]
        pending = self._store.pending_approvals_for_key(key)
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
        rate_limits = await self._read_rate_limits(record.workspace_path)
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
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
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
        if subcommand == "clear":
            if len(argv) < 2:
                await self._send_message(
                    message.chat_id,
                    "Usage: /files clear inbox|outbox|all",
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
        if subcommand == "send":
            if len(argv) < 2:
                await self._send_message(
                    message.chat_id,
                    "Usage: /files send <filename>",
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
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
        record = self._router.get_topic(key)
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
        self._refresh_workspace_id(key, record)
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
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
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
        self, workspace_path: Optional[str]
    ) -> Optional[dict[str, Any]]:
        client = await self._client_for_workspace(workspace_path)
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
        record = self._router.ensure_topic(message.chat_id, message.thread_id)
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
            self._router.set_approval_mode(message.chat_id, message.thread_id, "yolo")
            self._router.update_topic(
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
            self._router.set_approval_mode(message.chat_id, message.thread_id, "safe")
            self._router.update_topic(
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
            self._router.update_topic(
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
            self._router.update_topic(
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
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
        self._model_options.pop(key, None)
        self._model_pending.pop(key, None)
        record = self._router.get_topic(key)
        client = await self._client_for_workspace(
            record.workspace_path if record else None
        )
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
                    "model/list",
                    {"cursor": None, "limit": DEFAULT_MODEL_LIST_LIMIT},
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.model.list.failed",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
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
            options = _coerce_model_options(result)
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
                    _format_model_list(result),
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
                result = await client.request(
                    "model/list",
                    {"cursor": None, "limit": DEFAULT_MODEL_LIST_LIMIT},
                )
            except Exception as exc:
                log_event(
                    self._logger,
                    logging.WARNING,
                    "telegram.model.list.failed",
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
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
                _format_model_list(result),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if argv[0].lower() in ("clear", "reset"):
            self._router.update_topic(
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
        if effort and effort not in VALID_REASONING_EFFORTS:
            await self._send_message(
                message.chat_id,
                f"Unknown effort '{effort}'. Allowed: {', '.join(sorted(VALID_REASONING_EFFORTS))}.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        self._router.update_topic(
            message.chat_id,
            message.thread_id,
            lambda record: _set_model_overrides(
                record,
                model,
                effort=effort,
            ),
        )
        effort_note = f" (effort={effort})" if effort else ""
        await self._send_message(
            message.chat_id,
            f"Model set to {model}{effort_note}. Will apply on the next turn.",
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
        client = await self._client_for_workspace(record.workspace_path)
        if client is None:
            await self._send_message(
                message.chat_id,
                "Topic not bound. Use /bind <repo_id> or /bind <path>.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        log_event(
            self._logger,
            logging.INFO,
            "telegram.review.starting",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            codex_thread_id=thread_id,
            delivery=delivery,
            target=target.get("type"),
        )
        approval_policy, sandbox_policy = self._effective_policies(record)
        review_kwargs: dict[str, Any] = {}
        if approval_policy:
            review_kwargs["approval_policy"] = approval_policy
        if sandbox_policy:
            review_kwargs["sandbox_policy"] = sandbox_policy
        if record.model:
            review_kwargs["model"] = record.model
        if record.effort:
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
            )
            queue_started_at = time.monotonic()
            async with turn_semaphore:
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
                ctx = TurnContext(
                    topic_key=self._resolve_topic_key(
                        message.chat_id, message.thread_id
                    ),
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
                result = await turn_handle.wait()
                if turn_started_at is not None:
                    turn_elapsed_seconds = time.monotonic() - turn_started_at
        except Exception as exc:
            if turn_handle is not None:
                if turn_key is not None:
                    self._turn_contexts.pop(turn_key, None)
            runtime.current_turn_id = None
            runtime.current_turn_key = None
            runtime.interrupt_requested = False
            failure_message = "Codex review failed; check logs for details."
            if isinstance(exc, CodexAppServerDisconnected):
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
                self._router.update_topic(
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
        response_sent = await self._deliver_turn_response(
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            reply_to=message.message_id,
            placeholder_id=placeholder_id,
            response=response,
        )
        turn_id = turn_handle.turn_id if turn_handle else None
        token_usage = self._token_usage_by_turn.get(turn_id) if turn_id else None
        if token_usage is None and thread_id:
            token_usage = self._token_usage_by_thread.get(thread_id)
        await self._send_turn_metrics(
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            reply_to=message.message_id,
            elapsed_seconds=turn_elapsed_seconds,
            token_usage=token_usage,
        )
        if turn_id:
            self._token_usage_by_turn.pop(turn_id, None)
        if response_sent:
            await self._delete_message(message.chat_id, placeholder_id)
        await self._flush_outbox_files(
            record,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _handle_review(
        self, message: TelegramMessage, args: str, runtime: Any
    ) -> None:
        record = await self._require_bound_record(message)
        if not record:
            return
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
        raw_args = args.strip()
        delivery = "inline"
        token, remainder = _consume_raw_token(raw_args)
        if token and token.lower() in ("detached", "--detached"):
            delivery = "detached"
            raw_args = remainder
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
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
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
        client = await self._client_for_workspace(record.workspace_path)
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
        client = await self._client_for_workspace(record.workspace_path)
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
        client = await self._client_for_workspace(record.workspace_path)
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
            thread = await client.thread_start(record.workspace_path)
        except Exception as exc:
            log_event(
                self._logger,
                logging.WARNING,
                "telegram.compact.thread_start.failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            return False, "Failed to start a new Codex thread."
        if not await self._require_thread_workspace(
            message, record.workspace_path, thread, action="thread_start"
        ):
            return False, "Failed to start a new Codex thread."
        new_thread_id = _extract_thread_id(thread)
        if not new_thread_id:
            return False, "Failed to start a new Codex thread."
        log_event(
            self._logger,
            logging.INFO,
            "telegram.compact.apply.thread_started",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            codex_thread_id=new_thread_id,
        )
        record = self._apply_thread_result(
            message.chat_id,
            message.thread_id,
            thread,
            active_thread_id=new_thread_id,
        )
        seed_text = self._build_compact_seed_prompt(summary_text)
        record = self._router.update_topic(
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
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
        if not record.active_thread_id:
            await self._send_message(
                message.chat_id,
                "No active thread to compact. Use /new to start one.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        conflict_key = self._find_thread_conflict(record.active_thread_id, key=key)
        if conflict_key:
            self._router.set_active_thread(message.chat_id, message.thread_id, None)
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
        record = self._router.get_topic(key)
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
        record = self._router.get_topic(
            self._resolve_topic_key(message.chat_id, message.thread_id)
        )
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
            self._router.update_topic(
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
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
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
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
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
        key = self._resolve_topic_key(message.chat_id, message.thread_id)
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

    async def _handle_quit(
        self, message: TelegramMessage, _args: str, _runtime: Any
    ) -> None:
        await self._send_message(
            message.chat_id,
            "This command is not applicable in Telegram. Use /new to start fresh or /resume to switch threads.",
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )
