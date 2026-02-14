"""GitHub/PR command handlers for Telegram integration."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import httpx

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
from .....core.logging_utils import log_event
from .....core.state import now_iso
from .....core.text_delta_coalescer import TextDeltaCoalescer
from ....app_server.client import (
    CodexAppServerDisconnected,
)
from ...adapter import (
    InlineButton,
    TelegramMessage,
    build_inline_keyboard,
    encode_cancel_callback,
)
from ...config import AppServerUnavailableError
from ...constants import (
    MAX_TOPIC_THREAD_HISTORY,
    PLACEHOLDER_TEXT,
    QUEUED_PLACEHOLDER_TEXT,
    RESUME_PREVIEW_ASSISTANT_LIMIT,
    REVIEW_COMMIT_PICKER_PROMPT,
    TurnKey,
)
from ...helpers import (
    _compact_preview,
    _compose_agent_response,
    _compose_interrupt_response,
    _consume_raw_token,
    _extract_command_result,
    _format_review_commit_label,
    _parse_review_commit_log,
    _preview_from_text,
    _set_thread_summary,
    _with_conversation_id,
    format_public_error,
    is_interrupt_status,
)
from ...types import ReviewCommitSelectionState, TurnContext
from ..utils import _build_opencode_token_usage

if TYPE_CHECKING:
    from ...state import TelegramTopicRecord

from .shared import SharedHelpers


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


@dataclass
class CodexReviewSetup:
    """Prepared client and review payload for Codex reviews."""

    client: Any
    agent: str
    review_kwargs: dict[str, Any]


@dataclass
class CodexTurnContext:
    """State for an in-flight Codex review turn."""

    placeholder_id: Optional[int]
    turn_handle: Any
    turn_key: Optional[TurnKey]
    turn_semaphore: asyncio.Semaphore
    turn_started_at: Optional[float]
    queued: bool
    turn_elapsed_seconds: Optional[float] = None
    turn_slot_acquired: bool = False


@dataclass
class OpencodeReviewSetup:
    """Prepared context for OpenCode reviews."""

    supervisor: Any
    client: Any
    workspace_root: Path
    review_session_id: str
    permission_policy: str
    review_args: str


@dataclass
class OpencodeTurnContext:
    """State for an in-flight OpenCode review."""

    placeholder_id: Optional[int]
    turn_key: Optional[TurnKey]
    turn_id: Optional[str]
    review_session_id: str
    turn_semaphore: asyncio.Semaphore
    turn_started_at: Optional[float]
    turn_elapsed_seconds: Optional[float] = None
    queued: bool = False
    turn_slot_acquired: bool = False


class GitHubCommands(SharedHelpers):
    """GitHub/PR command handlers for Telegram integration.

    This class is designed to be used as a mixin in command handler classes.
    All methods use `self` to access instance attributes.
    """

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
        setup = await self._prepare_codex_review_setup(message, record)
        if setup is None:
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
            agent=setup.agent,
        )
        turn_context: Optional[CodexTurnContext] = None
        result = None
        try:
            turn_context = await self._launch_codex_review_turn(
                message,
                runtime,
                record,
                thread_id,
                target,
                delivery,
                setup,
            )
            if turn_context is None:
                return
            result = await self._wait_for_codex_review_result(
                message,
                setup,
                turn_context,
            )
        except Exception as exc:
            await self._handle_codex_review_failure(
                message,
                exc,
                turn_context,
            )
            return
        finally:
            await self._cleanup_codex_review_turn(turn_context, runtime)
        if result is None:
            return
        await self._finalize_codex_review_success(
            message,
            record,
            thread_id,
            result,
            turn_context,
            runtime,
        )

    async def _prepare_codex_review_setup(
        self, message: TelegramMessage, record: "TelegramTopicRecord"
    ) -> Optional[CodexReviewSetup]:
        """Prepare client and review kwargs for a Codex review."""
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
        agent = self._effective_agent(record)
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
        return CodexReviewSetup(
            client=client,
            agent=agent,
            review_kwargs=review_kwargs,
        )

    async def _launch_codex_review_turn(
        self,
        message: TelegramMessage,
        runtime: Any,
        record: "TelegramTopicRecord",
        thread_id: str,
        target: dict[str, Any],
        delivery: str,
        setup: CodexReviewSetup,
    ) -> Optional[CodexTurnContext]:
        """Send placeholder, acquire slot, and start the Codex review turn."""
        placeholder_id: Optional[int] = None
        turn_handle = None
        turn_key: Optional[TurnKey] = None
        turn_semaphore = self._ensure_turn_semaphore()
        queued = turn_semaphore.locked()
        placeholder_text = QUEUED_PLACEHOLDER_TEXT if queued else PLACEHOLDER_TEXT
        placeholder_id = await self._send_placeholder(
            message.chat_id,
            thread_id=message.thread_id,
            reply_to=message.message_id,
            text=placeholder_text,
            reply_markup=self._interrupt_keyboard(),
        )
        turn_context = CodexTurnContext(
            placeholder_id=placeholder_id,
            turn_handle=None,
            turn_key=None,
            turn_semaphore=turn_semaphore,
            turn_started_at=None,
            queued=queued,
            turn_elapsed_seconds=None,
            turn_slot_acquired=False,
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
            return None
        turn_context.turn_slot_acquired = True
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
            turn_handle = await setup.client.review_start(
                thread_id,
                target=target,
                delivery=delivery,
                **setup.review_kwargs,
            )
            turn_context.turn_handle = turn_handle
            turn_context.turn_started_at = time.monotonic()
            turn_key = self._turn_key(thread_id, turn_handle.turn_id)
            turn_context.turn_key = turn_key
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
                if turn_context.turn_slot_acquired:
                    turn_semaphore.release()
                return None
            await self._start_turn_progress(
                turn_key,
                ctx=ctx,
                agent=self._effective_agent(record),
                model=record.model,
                label="working",
            )
            return turn_context
        except Exception:
            if placeholder_id is not None:
                with suppress(Exception):
                    await self._delete_message(message.chat_id, placeholder_id)
            if turn_context.turn_slot_acquired:
                turn_semaphore.release()
            raise

    async def _wait_for_codex_review_result(
        self,
        message: TelegramMessage,
        setup: CodexReviewSetup,
        turn_context: CodexTurnContext,
    ):
        """Wait for the Codex review to finish and record timing."""
        if turn_context.turn_handle is None:
            return None
        topic_key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        result = await self._wait_for_turn_result(
            setup.client,
            turn_context.turn_handle,
            timeout_seconds=self._config.agent_turn_timeout_seconds.get("codex"),
            topic_key=topic_key,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
        )
        if turn_context.turn_started_at is not None:
            turn_context.turn_elapsed_seconds = (
                time.monotonic() - turn_context.turn_started_at
            )
        return result

    async def _handle_codex_review_failure(
        self,
        message: TelegramMessage,
        exc: Exception,
        turn_context: Optional[CodexTurnContext],
    ) -> None:
        """Send failure feedback when a Codex review fails."""
        placeholder_id = turn_context.placeholder_id if turn_context else None
        turn_handle = turn_context.turn_handle if turn_context else None
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
        if response_sent and placeholder_id is not None:
            await self._delete_message(message.chat_id, placeholder_id)

    async def _cleanup_codex_review_turn(
        self, turn_context: Optional[CodexTurnContext], runtime: Any
    ) -> None:
        """Clear turn bookkeeping and release any held semaphore."""
        if turn_context is not None:
            if turn_context.turn_key is not None:
                self._turn_contexts.pop(turn_context.turn_key, None)
                self._clear_thinking_preview(turn_context.turn_key)
                self._clear_turn_progress(turn_context.turn_key)
            if turn_context.turn_slot_acquired:
                turn_context.turn_semaphore.release()
        runtime.current_turn_id = None
        runtime.current_turn_key = None
        runtime.interrupt_requested = False

    async def _finalize_codex_review_success(
        self,
        message: TelegramMessage,
        record: "TelegramTopicRecord",
        thread_id: str,
        result: Any,
        turn_context: CodexTurnContext,
        runtime: Any,
    ) -> None:
        """Handle successful Codex review completion."""
        response = _compose_agent_response(
            getattr(result, "final_message", None),
            messages=result.agent_messages,
            errors=result.errors,
            status=result.status,
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
        turn_handle_id = (
            turn_context.turn_handle.turn_id if turn_context.turn_handle else None
        )
        if is_interrupt_status(result.status):
            response = _compose_interrupt_response(response)
            if (
                turn_handle_id
                and runtime.interrupt_message_id is not None
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
            turn_id=turn_handle_id,
            agent_message_count=len(result.agent_messages),
            error_count=len(result.errors),
        )
        turn_id = turn_handle_id
        token_usage = self._token_usage_by_turn.get(turn_id) if turn_id else None
        metrics = self._format_turn_metrics_text(
            token_usage, turn_context.turn_elapsed_seconds
        )
        metrics_mode = self._metrics_mode()
        response_text = response
        if metrics and metrics_mode == "append_to_response":
            response_text = f"{response_text}\n\n{metrics}"
        response_sent = await self._deliver_turn_response(
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            reply_to=message.message_id,
            placeholder_id=turn_context.placeholder_id,
            response=response_text,
        )
        placeholder_handled = False
        if metrics and metrics_mode == "separate":
            await self._send_turn_metrics(
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                reply_to=message.message_id,
                elapsed_seconds=turn_context.turn_elapsed_seconds,
                token_usage=token_usage,
            )
        elif metrics and metrics_mode == "append_to_progress" and response_sent:
            placeholder_handled = await self._append_metrics_to_placeholder(
                message.chat_id, turn_context.placeholder_id, metrics
            )
        if turn_id:
            self._token_usage_by_turn.pop(turn_id, None)
        if response_sent and not placeholder_handled:
            await self._delete_message(message.chat_id, turn_context.placeholder_id)
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
        setup = await self._prepare_opencode_review_setup(
            message, record, thread_id, target, delivery
        )
        if setup is None:
            return
        agent = self._effective_agent(record)
        log_event(
            self._logger,
            logging.INFO,
            "telegram.review.starting",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            codex_thread_id=setup.review_session_id,
            delivery=delivery,
            target=target.get("type"),
            agent=agent,
        )
        await self._run_opencode_review_flow(
            message,
            runtime,
            record,
            setup,
        )

    async def _prepare_opencode_review_setup(
        self,
        message: TelegramMessage,
        record: TelegramTopicRecord,
        thread_id: str,
        target: dict[str, Any],
        delivery: str,
    ) -> Optional[OpencodeReviewSetup]:
        """Prepare supervisor, client, and session id for an OpenCode review."""
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
            return None
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
                return None
            review_session_id = extract_session_id(session, allow_fallback_id=True)
            if not review_session_id:
                await self._send_message(
                    message.chat_id,
                    "Failed to start a new OpenCode thread.",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
                return None

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
        approval_policy, _sandbox_policy = self._effective_policies(record)
        permission_policy = map_approval_policy_to_permission(
            approval_policy, default=PERMISSION_ALLOW
        )
        review_args = _opencode_review_arguments(target)
        return OpencodeReviewSetup(
            supervisor=supervisor,
            client=opencode_client,
            workspace_root=workspace_root,
            review_session_id=review_session_id,
            permission_policy=permission_policy,
            review_args=review_args,
        )

    async def _run_opencode_review_flow(
        self,
        message: TelegramMessage,
        runtime: Any,
        record: TelegramTopicRecord,
        setup: OpencodeReviewSetup,
    ) -> None:
        """Orchestrate the OpenCode review turn."""
        turn_context: Optional[OpencodeTurnContext] = None
        output_result = None
        try:
            turn_context, output_result = await self._execute_opencode_review_turn(
                message, runtime, record, setup
            )
        except Exception as exc:
            await self._handle_opencode_review_failure(
                message,
                setup,
                exc,
                turn_context,
            )
            return
        finally:
            await self._cleanup_opencode_review_turn(turn_context, runtime)
        if output_result is None or turn_context is None:
            return
        await self._finalize_opencode_review_success(
            message,
            record,
            setup,
            turn_context,
            output_result,
        )

    async def _execute_opencode_review_turn(
        self,
        message: TelegramMessage,
        runtime: Any,
        record: TelegramTopicRecord,
        setup: OpencodeReviewSetup,
    ) -> tuple[Optional[OpencodeTurnContext], Optional[Any]]:
        """Run the OpenCode review turn and stream progress."""
        placeholder_id: Optional[int] = None
        turn_key: Optional[TurnKey] = None
        turn_id: Optional[str] = None
        output_result = None
        turn_semaphore = self._ensure_turn_semaphore()
        queued = turn_semaphore.locked()
        placeholder_text = QUEUED_PLACEHOLDER_TEXT if queued else PLACEHOLDER_TEXT
        placeholder_id = await self._send_placeholder(
            message.chat_id,
            thread_id=message.thread_id,
            reply_to=message.message_id,
            text=placeholder_text,
            reply_markup=self._interrupt_keyboard(),
        )
        turn_context = OpencodeTurnContext(
            placeholder_id=placeholder_id,
            turn_key=None,
            turn_id=None,
            review_session_id=setup.review_session_id,
            turn_semaphore=turn_semaphore,
            turn_started_at=None,
            queued=queued,
            turn_slot_acquired=False,
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
            if turn_semaphore.locked():
                turn_semaphore.release()
            return None, None
        turn_context.turn_slot_acquired = True
        opencode_turn_started = False
        turn_started_at: Optional[float] = None
        try:
            queue_wait_ms = int((time.monotonic() - queue_started_at) * 1000)
            log_event(
                self._logger,
                logging.INFO,
                "telegram.review.queue_wait",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                codex_thread_id=setup.review_session_id,
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
            try:
                await setup.supervisor.mark_turn_started(setup.workspace_root)
                opencode_turn_started = True
                model_payload = split_model_id(record.model)
                missing_env = await opencode_missing_env(
                    setup.client,
                    str(setup.workspace_root),
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
                    return turn_context, None
                turn_started_at = time.monotonic()
                turn_id = build_turn_id(setup.review_session_id)
                self._token_usage_by_thread.pop(setup.review_session_id, None)
                runtime.current_turn_id = turn_id
                runtime.current_turn_key = (setup.review_session_id, turn_id)
                topic_key = await self._resolve_topic_key(
                    message.chat_id, message.thread_id
                )
                ctx = TurnContext(
                    topic_key=topic_key,
                    chat_id=message.chat_id,
                    thread_id=message.thread_id,
                    codex_thread_id=setup.review_session_id,
                    reply_to_message_id=message.message_id,
                    placeholder_message_id=placeholder_id,
                )
                turn_key = self._turn_key(setup.review_session_id, turn_id)
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
                    return turn_context, None
                turn_context.turn_key = turn_key
                turn_context.turn_id = turn_id
                turn_context.turn_started_at = turn_started_at
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
                    if setup.permission_policy != PERMISSION_ASK:
                        return "reject"
                    prompt = format_permission_prompt(props)
                    decision = await self._handle_approval_request(
                        {
                            "id": request_id,
                            "method": "opencode/permission/requestApproval",
                            "params": {
                                "turnId": turn_id,
                                "threadId": setup.review_session_id,
                                "prompt": prompt,
                            },
                        }
                    )
                    return decision

                abort_requested = False

                async def _abort_opencode() -> None:
                    try:
                        await asyncio.wait_for(
                            setup.client.abort(setup.review_session_id), timeout=10
                        )
                    except Exception:
                        pass

                def _should_stop() -> bool:
                    nonlocal abort_requested
                    if runtime.interrupt_requested and not abort_requested:
                        abort_requested = True
                        asyncio.create_task(_abort_opencode())
                    return runtime.interrupt_requested

                reasoning_buffers: dict[str, TextDeltaCoalescer] = {}
                watched_session_ids = {setup.review_session_id}
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
                        session_id = setup.review_session_id
                    is_primary_session = session_id == setup.review_session_id
                    subagent_label = subagent_labels.get(session_id)
                    if part_type == "reasoning":
                        part_id = part.get("id") or part.get("partId") or "reasoning"
                        buffer_key = f"{session_id}:{part_id}"
                        if buffer_key not in reasoning_buffers:
                            reasoning_buffers[buffer_key] = TextDeltaCoalescer()
                        coalescer = reasoning_buffers[buffer_key]
                        if delta_text:
                            coalescer.add(delta_text)
                        else:
                            raw_text = part.get("text")
                            if isinstance(raw_text, str) and raw_text:
                                coalescer.add(raw_text)
                        buffer = coalescer.get_buffer()
                        if buffer:
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
                                if isinstance(description, str) and description.strip():
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
                                    if part_text not in seen and not seen.add(part_text)
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
                                subagent_labels.setdefault(session_id, subagent_label)
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
                                last_usage = token_usage.get("last")
                                if isinstance(last_usage, dict):
                                    token_usage["total"] = dict(last_usage)
                                if (
                                    "modelContextWindow" not in token_usage
                                    and not context_window_resolved
                                ):
                                    opencode_context_window = await self._resolve_opencode_model_context_window(
                                        setup.client,
                                        setup.workspace_root,
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
                                    thread_id=setup.review_session_id,
                                )
                                await self._note_progress_context_usage(
                                    token_usage,
                                    turn_id=turn_id,
                                    thread_id=setup.review_session_id,
                                )
                    await self._schedule_progress_edit(turn_key)

                ready_event = asyncio.Event()
                output_task = asyncio.create_task(
                    collect_opencode_output(
                        setup.client,
                        session_id=setup.review_session_id,
                        workspace_path=str(setup.workspace_root),
                        model_payload=model_payload,
                        progress_session_ids=watched_session_ids,
                        permission_policy=setup.permission_policy,
                        permission_handler=(
                            _permission_handler
                            if setup.permission_policy == PERMISSION_ASK
                            else None
                        ),
                        should_stop=_should_stop,
                        part_handler=_handle_opencode_part,
                        ready_event=ready_event,
                        stall_timeout_seconds=self._opencode_session_stall_timeout_seconds(),
                    )
                )
                with suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(ready_event.wait(), timeout=2.0)
                timeout_seconds = self._config.agent_turn_timeout_seconds.get(
                    "opencode"
                )
                timeout_task: Optional[asyncio.Task] = None
                if timeout_seconds is not None and timeout_seconds > 0:
                    timeout_task = asyncio.create_task(asyncio.sleep(timeout_seconds))
                command_task = asyncio.create_task(
                    setup.client.send_command(
                        setup.review_session_id,
                        command="review",
                        arguments=setup.review_args,
                        model=record.model,
                    )
                )
                try:
                    await command_task
                except Exception as exc:
                    if timeout_task is not None:
                        timeout_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await timeout_task
                    output_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await output_task
                    raise exc
                if timeout_task is not None:
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
                        timeout_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await timeout_task
                        turn_context.turn_elapsed_seconds = (
                            time.monotonic() - turn_started_at
                            if turn_started_at is not None
                            else None
                        )
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
                        return turn_context, None
                    timeout_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await timeout_task
                output_result = await output_task
                elapsed = (
                    time.monotonic() - turn_started_at
                    if turn_started_at is not None
                    else None
                )
                turn_context.turn_elapsed_seconds = elapsed
                return turn_context, output_result
            finally:
                if opencode_turn_started:
                    await setup.supervisor.mark_turn_finished(setup.workspace_root)
        finally:
            runtime.current_turn_id = None
            runtime.current_turn_key = None
            runtime.interrupt_requested = False

    async def _handle_opencode_review_failure(
        self,
        message: TelegramMessage,
        setup: OpencodeReviewSetup,
        exc: Exception,
        turn_context: Optional[OpencodeTurnContext],
    ) -> None:
        """Send failure feedback for OpenCode review errors."""
        placeholder_id = turn_context.placeholder_id if turn_context else None
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
        if response_sent and placeholder_id is not None:
            await self._delete_message(message.chat_id, placeholder_id)

    async def _cleanup_opencode_review_turn(
        self, turn_context: Optional[OpencodeTurnContext], runtime: Any
    ) -> None:
        """Clear state after an OpenCode review turn completes."""
        if turn_context is not None:
            if turn_context.turn_key is not None:
                self._turn_contexts.pop(turn_context.turn_key, None)
                self._clear_thinking_preview(turn_context.turn_key)
                self._clear_turn_progress(turn_context.turn_key)
            if turn_context.turn_slot_acquired:
                turn_context.turn_semaphore.release()
        runtime.current_turn_id = None
        runtime.current_turn_key = None
        runtime.interrupt_requested = False

    async def _finalize_opencode_review_success(
        self,
        message: TelegramMessage,
        record: TelegramTopicRecord,
        setup: OpencodeReviewSetup,
        turn_context: OpencodeTurnContext,
        output_result: Any,
    ) -> None:
        """Send OpenCode review results and clean up placeholders."""
        output = output_result.text
        if output_result.error:
            failure_message = f"OpenCode review failed: {output_result.error}"
            response_sent = await self._deliver_turn_response(
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                reply_to=message.message_id,
                placeholder_id=turn_context.placeholder_id,
                response=failure_message,
            )
            if response_sent:
                await self._delete_message(message.chat_id, turn_context.placeholder_id)
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
                        setup.review_session_id,
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
            turn_id=turn_context.turn_id,
        )
        token_usage = (
            self._token_usage_by_turn.get(turn_context.turn_id)
            if turn_context.turn_id
            else None
        )
        metrics = self._format_turn_metrics_text(
            token_usage, turn_context.turn_elapsed_seconds
        )
        metrics_mode = self._metrics_mode()
        response_text = output or "No response."
        if metrics and metrics_mode == "append_to_response":
            response_text = f"{response_text}\n\n{metrics}"
        response_sent = await self._deliver_turn_response(
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            reply_to=message.message_id,
            placeholder_id=turn_context.placeholder_id,
            response=response_text,
        )
        placeholder_handled = False
        if metrics and metrics_mode == "separate":
            await self._send_turn_metrics(
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                reply_to=message.message_id,
                elapsed_seconds=turn_context.turn_elapsed_seconds,
                token_usage=token_usage,
            )
        elif metrics and metrics_mode == "append_to_progress" and response_sent:
            placeholder_handled = await self._append_metrics_to_placeholder(
                message.chat_id, turn_context.placeholder_id, metrics
            )
        if turn_context.turn_id:
            self._token_usage_by_turn.pop(turn_context.turn_id, None)
        if response_sent and not placeholder_handled:
            await self._delete_message(message.chat_id, turn_context.placeholder_id)
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


def _format_opencode_exception(exc: Exception) -> Optional[str]:
    """Format OpenCode exceptions for user-friendly error messages."""
    from .....agents.opencode.client import OpenCodeProtocolError
    from .....agents.opencode.supervisor import OpenCodeSupervisorError

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
            detail = _extract_opencode_error_detail(exc.response.json())
        except Exception:
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


def _extract_opencode_error_detail(payload: Any) -> Optional[str]:
    """Extract error detail from OpenCode response payload."""
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
