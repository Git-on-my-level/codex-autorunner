"""GitHub/PR command handlers for Telegram integration."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

import httpx

from .....agents.base import harness_progress_event_stream
from .....agents.opencode.harness import OpenCodeHarness
from .....agents.opencode.runtime import (
    extract_session_id,
    format_permission_prompt,
    opencode_missing_env,
    split_model_id,
)
from .....core.logging_utils import log_event
from .....core.orchestration.runtime_thread_events import (
    RuntimeThreadRunEventState,
    normalize_runtime_thread_raw_event,
)
from .....core.ports.run_event import TokenUsage
from .....core.state import now_iso
from ....app_server.client import (
    CodexAppServerDisconnected,
)
from ....chat.constants import (
    APP_SERVER_UNAVAILABLE_MESSAGE,
    TOPIC_NOT_BOUND_MESSAGE,
)
from ....chat.turn_metrics import compose_turn_response_with_footer
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
    _compose_agent_response,
    _compose_interrupt_response,
    _consume_raw_token,
    _extract_command_result,
    _format_review_commit_label,
    _parse_review_commit_log,
    _preview_from_text,
    _set_thread_summary,
    _with_conversation_id,
    is_interrupt_status,
)
from ...types import ReviewCommitSelectionState, TurnContext

if TYPE_CHECKING:
    from ...state import TelegramTopicRecord

from .command_utils import _format_opencode_exception, _opencode_review_arguments
from .shared import TelegramCommandSupportMixin


@dataclass
class ReviewTurnContext:
    """Unified state for an in-flight review turn (Codex or OpenCode)."""

    placeholder_id: Optional[int]
    turn_key: Optional[TurnKey]
    turn_id: Optional[str]
    turn_semaphore: asyncio.Semaphore
    turn_started_at: Optional[float]
    turn_elapsed_seconds: Optional[float]
    queued: bool
    turn_slot_acquired: bool
    agent_handle: Optional[Any] = None
    review_session_id: Optional[str] = None


@dataclass
class CodexReviewSetup:
    """Prepared client and review payload for Codex reviews."""

    client: Any
    agent: str
    review_kwargs: dict[str, Any]


@dataclass
class OpencodeReviewSetup:
    """Prepared context for OpenCode reviews."""

    supervisor: Any
    client: Any
    workspace_root: Path
    review_session_id: str
    approval_mode: Optional[str]
    sandbox_policy: Optional[Any]
    review_args: str


class GitHubCommands(TelegramCommandSupportMixin):
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
        turn_context: Optional[ReviewTurnContext] = None
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
        except (
            RuntimeError,
            OSError,
            ConnectionError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            asyncio.TimeoutError,
        ) as exc:
            await self._handle_codex_review_failure(
                message,
                exc,
                turn_context,
            )
            return
        finally:
            await self._cleanup_review_turn(turn_context, runtime)
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
                APP_SERVER_UNAVAILABLE_MESSAGE,
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return None
        if client is None:
            await self._send_message(
                message.chat_id,
                TOPIC_NOT_BOUND_MESSAGE,
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

    async def _acquire_review_turn_slot(
        self,
        message: TelegramMessage,
        runtime: Any,
        *,
        session_id: str,
        agent_handle: Optional[Any] = None,
        review_session_id: Optional[str] = None,
    ) -> Optional[tuple[ReviewTurnContext, float]]:
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
        turn_context = ReviewTurnContext(
            placeholder_id=placeholder_id,
            turn_key=None,
            turn_id=None,
            turn_semaphore=turn_semaphore,
            turn_started_at=None,
            turn_elapsed_seconds=None,
            queued=queued,
            turn_slot_acquired=False,
            agent_handle=agent_handle,
            review_session_id=review_session_id,
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
        queue_wait_ms = int((time.monotonic() - queue_started_at) * 1000)
        log_event(
            self._logger,
            logging.INFO,
            "telegram.review.queue_wait",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            codex_thread_id=session_id,
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
        return turn_context, queue_started_at

    async def _register_review_turn_context(
        self,
        message: TelegramMessage,
        record: "TelegramTopicRecord",
        runtime: Any,
        turn_context: ReviewTurnContext,
        session_id: str,
        turn_id: str,
        agent_label: str,
    ) -> bool:
        turn_key = self._turn_key(session_id, turn_id)
        turn_context.turn_key = turn_key
        turn_context.turn_id = turn_id
        runtime.current_turn_id = turn_id
        runtime.current_turn_key = turn_key
        topic_key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        ctx = TurnContext(
            topic_key=topic_key,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            codex_thread_id=session_id,
            reply_to_message_id=message.message_id,
            placeholder_message_id=turn_context.placeholder_id,
        )
        if turn_key is None or not self._register_turn_context(turn_key, turn_id, ctx):
            runtime.current_turn_id = None
            runtime.current_turn_key = None
            runtime.interrupt_requested = False
            await self._send_message(
                message.chat_id,
                "Turn collision detected; please retry.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            if turn_context.placeholder_id is not None:
                await self._delete_message(message.chat_id, turn_context.placeholder_id)
            if turn_context.turn_slot_acquired:
                turn_context.turn_semaphore.release()
            return False
        await self._start_turn_progress(
            turn_key,
            ctx=ctx,
            agent=self._effective_agent(record),
            model=record.model,
            label=agent_label,
        )
        return True

    async def _launch_codex_review_turn(
        self,
        message: TelegramMessage,
        runtime: Any,
        record: "TelegramTopicRecord",
        thread_id: str,
        target: dict[str, Any],
        delivery: str,
        setup: CodexReviewSetup,
    ) -> Optional[ReviewTurnContext]:
        slot_result = await self._acquire_review_turn_slot(
            message, runtime, session_id=thread_id
        )
        if slot_result is None:
            return None
        turn_context, _queue_started_at = slot_result
        try:
            turn_handle = await setup.client.review_start(
                thread_id,
                target=target,
                delivery=delivery,
                **setup.review_kwargs,
            )
            turn_context.agent_handle = turn_handle
            turn_context.turn_started_at = time.monotonic()
            if not await self._register_review_turn_context(
                message,
                record,
                runtime,
                turn_context,
                thread_id,
                turn_handle.turn_id,
                "working",
            ):
                return None
            return turn_context
        except (
            RuntimeError,
            OSError,
            ConnectionError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            asyncio.CancelledError,
        ):  # intentional: cleanup + re-raise
            if turn_context.placeholder_id is not None:
                with suppress(Exception):  # intentional: best-effort cleanup
                    await self._delete_message(
                        message.chat_id, turn_context.placeholder_id
                    )
            if turn_context.turn_slot_acquired:
                turn_context.turn_semaphore.release()
            raise

    async def _wait_for_codex_review_result(
        self,
        message: TelegramMessage,
        setup: CodexReviewSetup,
        turn_context: ReviewTurnContext,
    ):
        if turn_context.agent_handle is None:
            return None
        topic_key = await self._resolve_topic_key(message.chat_id, message.thread_id)
        result = await self._wait_for_turn_result(
            setup.client,
            turn_context.agent_handle,
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
        turn_context: Optional[ReviewTurnContext],
    ) -> None:
        agent_handle = turn_context.agent_handle if turn_context else None
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
                turn_id=agent_handle.turn_id if agent_handle else None,
            )
            failure_message = (
                "Codex app-server disconnected; recovering now. "
                "Your review did not complete. Please resend the review command in a moment."
            )
        await self._handle_review_failure(
            message,
            exc,
            turn_context,
            failure_message=failure_message,
            reason=reason,
        )

    async def _cleanup_review_turn(
        self, turn_context: Optional[ReviewTurnContext], runtime: Any
    ) -> None:
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

    async def _handle_review_failure(
        self,
        message: TelegramMessage,
        exc: Exception,
        turn_context: Optional[ReviewTurnContext],
        *,
        failure_message: str,
        reason: str = "review_failed",
    ) -> None:
        placeholder_id = turn_context.placeholder_id if turn_context else None
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

    async def _update_review_topic_summary(
        self,
        message: TelegramMessage,
        session_id: str,
        preview_text: Optional[str],
        record: "TelegramTopicRecord",
    ) -> None:
        if not (session_id and preview_text):
            return
        await self._router.update_topic(
            message.chat_id,
            message.thread_id,
            lambda rec: _set_thread_summary(
                rec,
                session_id,
                assistant_preview=preview_text,
                last_used_at=now_iso(),
                workspace_path=rec.workspace_path,
                rollout_path=rec.rollout_path,
            ),
        )

    def _get_review_intermediate_response(self, turn_key: Optional[TurnKey]) -> str:
        if turn_key is None:
            return ""
        render_summary = getattr(self, "_render_turn_progress_summary", None)
        if callable(render_summary):
            return render_summary(turn_key)
        render_fn = getattr(self, "_render_final_turn_progress", None)
        if callable(render_fn):
            return render_fn(turn_key)
        return ""

    async def _deliver_review_final_response(
        self,
        message: TelegramMessage,
        record: "TelegramTopicRecord",
        turn_context: ReviewTurnContext,
        response_text: str,
        turn_id: Optional[str],
    ) -> bool:
        token_usage = self._token_usage_by_turn.get(turn_id) if turn_id else None
        intermediate_response = self._get_review_intermediate_response(
            turn_context.turn_key
        )
        response_text = compose_turn_response_with_footer(
            response_text,
            summary_text=intermediate_response,
            token_usage=token_usage,
            elapsed_seconds=turn_context.turn_elapsed_seconds,
            agent=self._effective_agent(record),
            model=getattr(record, "model", None),
        )
        try:
            response_sent = await self._deliver_turn_response(
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                reply_to=message.message_id,
                placeholder_id=turn_context.placeholder_id,
                response=response_text,
            )
        except TypeError as exc:
            if "intermediate_response" not in str(exc):
                raise
            response_sent = await self._deliver_turn_response(
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                reply_to=message.message_id,
                placeholder_id=turn_context.placeholder_id,
                response=response_text,
            )
        return response_sent

    async def _review_success_epilogue(
        self,
        message: TelegramMessage,
        record: "TelegramTopicRecord",
        turn_id: Optional[str],
    ) -> None:
        if turn_id:
            self._token_usage_by_turn.pop(turn_id, None)
        await self._flush_outbox_files(
            record,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    async def _finalize_codex_review_success(
        self,
        message: TelegramMessage,
        record: "TelegramTopicRecord",
        thread_id: str,
        result: Any,
        turn_context: ReviewTurnContext,
        runtime: Any,
    ) -> None:
        response = _compose_agent_response(
            getattr(result, "final_message", None),
            messages=result.agent_messages,
            errors=result.errors,
            status=result.status,
        )
        if thread_id and result.agent_messages:
            await self._update_review_topic_summary(
                message,
                thread_id,
                _preview_from_text(response, RESUME_PREVIEW_ASSISTANT_LIMIT),
                record,
            )
        turn_handle_id = (
            turn_context.agent_handle.turn_id if turn_context.agent_handle else None
        )
        interrupt_status_fallback_text = None
        if is_interrupt_status(result.status):
            response = _compose_interrupt_response(response)
            if (
                turn_handle_id
                and runtime.interrupt_message_id is not None
                and runtime.interrupt_turn_id == turn_handle_id
            ):
                interrupt_status_fallback_text = "Interrupted."
            runtime.interrupt_requested = False
        elif runtime.interrupt_turn_id == turn_handle_id:
            interrupt_status_fallback_text = "Interrupt requested; turn completed."
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
        response_sent = await self._deliver_review_final_response(
            message, record, turn_context, response, turn_handle_id
        )
        if interrupt_status_fallback_text and turn_handle_id:
            await self._clear_interrupt_status_message(
                chat_id=message.chat_id,
                runtime=runtime,
                turn_id=turn_handle_id,
                fallback_text=interrupt_status_fallback_text,
                outcome_visible=response_sent,
            )
        await self._review_success_epilogue(message, record, turn_handle_id)

    async def _run_opencode_review(
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
        log_event(
            self._logger,
            logging.INFO,
            "telegram.review.starting",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            codex_thread_id=setup.review_session_id,
            delivery=delivery,
            target=target.get("type"),
            agent=self._effective_agent(record),
        )
        turn_context: Optional[ReviewTurnContext] = None
        output_result = None
        try:
            turn_context, output_result = await self._execute_opencode_review_turn(
                message, runtime, record, setup
            )
        except (
            RuntimeError,
            OSError,
            ConnectionError,
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            asyncio.TimeoutError,
            httpx.HTTPError,
        ) as exc:
            await self._handle_opencode_review_failure(
                message,
                setup,
                exc,
                turn_context,
            )
            return
        finally:
            await self._cleanup_review_turn(turn_context, runtime)
        if output_result is None or turn_context is None:
            return
        await self._finalize_opencode_review_success(
            message,
            record,
            setup,
            turn_context,
            output_result,
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
        except (httpx.HTTPError, ConnectionError, OSError) as exc:
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
            except (httpx.HTTPError, ConnectionError, OSError, ValueError) as exc:
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
        approval_mode, sandbox_policy = self._effective_policies(record)
        review_args = _opencode_review_arguments(target)
        return OpencodeReviewSetup(
            supervisor=supervisor,
            client=opencode_client,
            workspace_root=workspace_root,
            review_session_id=review_session_id,
            approval_mode=approval_mode,
            sandbox_policy=sandbox_policy,
            review_args=review_args,
        )

    async def _execute_opencode_review_turn(
        self,
        message: TelegramMessage,
        runtime: Any,
        record: TelegramTopicRecord,
        setup: OpencodeReviewSetup,
    ) -> tuple[Optional[ReviewTurnContext], Optional[Any]]:
        slot_result = await self._acquire_review_turn_slot(
            message,
            runtime,
            session_id=setup.review_session_id,
            review_session_id=setup.review_session_id,
        )
        if slot_result is None:
            return None, None
        turn_context, _queue_started_at = slot_result
        output_result = None
        opencode_turn_started = False
        turn_started_at: Optional[float] = None
        try:
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
                        placeholder_id=turn_context.placeholder_id,
                        response=failure_message,
                    )
                    if response_sent:
                        await self._delete_message(
                            message.chat_id, turn_context.placeholder_id
                        )
                    return turn_context, None
                turn_started_at = time.monotonic()
                self._token_usage_by_thread.pop(setup.review_session_id, None)
                harness = OpenCodeHarness(setup.supervisor)
                turn_ref = await harness.start_review(
                    setup.workspace_root,
                    setup.review_session_id,
                    prompt=setup.review_args,
                    model=record.model,
                    reasoning=None,
                    approval_mode=setup.approval_mode,
                    sandbox_policy=setup.sandbox_policy,
                )
                turn_id = turn_ref.turn_id
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
                    placeholder_message_id=turn_context.placeholder_id,
                )
                turn_key = self._turn_key(setup.review_session_id, turn_id)
                if turn_key is None or not self._register_turn_context(
                    turn_key, turn_id, ctx
                ):
                    with suppress(Exception):  # intentional: best-effort cleanup
                        await harness.interrupt(
                            setup.workspace_root,
                            setup.review_session_id,
                            turn_id,
                        )
                    with suppress(Exception):  # intentional: best-effort cleanup
                        await harness.wait_for_turn(
                            setup.workspace_root,
                            setup.review_session_id,
                            turn_id,
                            timeout=5.0,
                        )
                    runtime.current_turn_id = None
                    runtime.current_turn_key = None
                    runtime.interrupt_requested = False
                    await self._send_message(
                        message.chat_id,
                        "Turn collision detected; please retry.",
                        thread_id=message.thread_id,
                        reply_to=message.message_id,
                    )
                    if turn_context.placeholder_id is not None:
                        await self._delete_message(
                            message.chat_id, turn_context.placeholder_id
                        )
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

                harness.configure_turn_handlers(
                    setup.review_session_id,
                    turn_id,
                    permission_handler=_permission_handler,
                )

                opencode_context_window: Optional[int] = None
                context_window_resolved = False

                async def _cache_run_event_usage(run_event: Any) -> None:
                    nonlocal opencode_context_window
                    nonlocal context_window_resolved
                    if not isinstance(run_event, TokenUsage):
                        return
                    token_usage = run_event.usage
                    if not isinstance(token_usage, dict):
                        return
                    token_usage = dict(token_usage)
                    last_usage = token_usage.get("last")
                    if isinstance(last_usage, dict):
                        token_usage["total"] = dict(last_usage)
                    if (
                        "modelContextWindow" not in token_usage
                        and not context_window_resolved
                    ):
                        opencode_context_window = (
                            await self._resolve_opencode_model_context_window(
                                setup.client,
                                setup.workspace_root,
                                model_payload,
                            )
                        )
                        context_window_resolved = True
                    if (
                        "modelContextWindow" not in token_usage
                        and isinstance(opencode_context_window, int)
                        and opencode_context_window > 0
                    ):
                        token_usage["modelContextWindow"] = opencode_context_window
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

                async def _pump_progress() -> None:
                    event_state = RuntimeThreadRunEventState()
                    try:
                        async for raw_event in harness_progress_event_stream(
                            harness,
                            setup.workspace_root,
                            setup.review_session_id,
                            turn_id,
                        ):
                            run_events = await normalize_runtime_thread_raw_event(
                                raw_event,
                                event_state,
                            )
                            for run_event in run_events:
                                await _cache_run_event_usage(run_event)
                                if turn_key is not None:
                                    await self._apply_run_event_to_progress(
                                        turn_key,
                                        run_event,
                                    )
                    except asyncio.CancelledError:
                        raise
                    except (
                        RuntimeError,
                        OSError,
                        ValueError,
                        TypeError,
                        KeyError,
                        AttributeError,
                        ConnectionError,
                    ):  # intentional: best-effort progress streaming
                        self._logger.warning(
                            "Telegram OpenCode review progress pump failed",
                            exc_info=True,
                        )

                async def _wait_for_interrupt_request() -> None:
                    while not runtime.interrupt_requested:
                        await asyncio.sleep(0.1)

                timeout_seconds = self._config.agent_turn_timeout_seconds.get(
                    "opencode"
                )
                wait_task = asyncio.create_task(
                    harness.wait_for_turn(
                        setup.workspace_root,
                        setup.review_session_id,
                        turn_id,
                    )
                )
                progress_task = asyncio.create_task(_pump_progress())
                interrupt_task: Optional[asyncio.Task[None]] = asyncio.create_task(
                    _wait_for_interrupt_request()
                )
                timeout_task: Optional[asyncio.Task[None]] = None
                if timeout_seconds is not None and timeout_seconds > 0:
                    timeout_task = asyncio.create_task(asyncio.sleep(timeout_seconds))
                try:
                    while True:
                        pending_tasks: set[asyncio.Task[Any]] = {wait_task}
                        if interrupt_task is not None:
                            pending_tasks.add(interrupt_task)
                        if timeout_task is not None:
                            pending_tasks.add(timeout_task)
                        done, _pending = await asyncio.wait(
                            pending_tasks,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if wait_task in done:
                            terminal_result = await wait_task
                            output_result = type(
                                "OpenCodeReviewResult",
                                (),
                                {
                                    "text": terminal_result.assistant_text,
                                    "error": (
                                        terminal_result.errors[0]
                                        if terminal_result.errors
                                        else None
                                    ),
                                },
                            )()
                            break
                        if timeout_task is not None and timeout_task in done:
                            runtime.interrupt_requested = True
                            await harness.interrupt(
                                setup.workspace_root,
                                setup.review_session_id,
                                turn_id,
                            )
                            wait_task.cancel()
                            with suppress(asyncio.CancelledError):
                                await wait_task
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
                                placeholder_id=turn_context.placeholder_id,
                                response=failure_message,
                            )
                            if response_sent:
                                await self._delete_message(
                                    message.chat_id, turn_context.placeholder_id
                                )
                            return turn_context, None
                        if interrupt_task is not None and interrupt_task in done:
                            await harness.interrupt(
                                setup.workspace_root,
                                setup.review_session_id,
                                turn_id,
                            )
                            interrupt_task.cancel()
                            with suppress(asyncio.CancelledError):
                                await interrupt_task
                            interrupt_task = None
                finally:
                    if interrupt_task is not None:
                        interrupt_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await interrupt_task
                    if timeout_task is not None:
                        timeout_task.cancel()
                        with suppress(asyncio.CancelledError):
                            await timeout_task
                    progress_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await progress_task
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
        turn_context: Optional[ReviewTurnContext],
    ) -> None:
        failure_message = (
            _format_opencode_exception(exc)
            or "OpenCode review failed; check logs for details."
        )
        await self._handle_review_failure(
            message,
            exc,
            turn_context,
            failure_message=failure_message,
        )

    async def _finalize_opencode_review_success(
        self,
        message: TelegramMessage,
        record: TelegramTopicRecord,
        setup: OpencodeReviewSetup,
        turn_context: ReviewTurnContext,
        output_result: Any,
    ) -> None:
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
            await self._update_review_topic_summary(
                message,
                setup.review_session_id,
                _preview_from_text(output, RESUME_PREVIEW_ASSISTANT_LIMIT),
                record,
            )
        log_event(
            self._logger,
            logging.INFO,
            "telegram.review.completed",
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            turn_id=turn_context.turn_id,
        )
        response_text = output or ""
        if not response_text.strip():
            response_text = ""
        await self._deliver_review_final_response(
            message, record, turn_context, response_text, turn_context.turn_id
        )
        await self._review_success_epilogue(message, record, turn_context.turn_id)

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
        if not self._agent_supports_capability(agent, "review"):
            supported = ", ".join(self._agents_supporting_capability("review"))
            await self._send_message(
                message.chat_id,
                (
                    f"{self._agent_display_name(agent)} does not support /review in Telegram."
                    + (
                        f" Switch to an agent with review support: {supported}."
                        if supported
                        else ""
                    )
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return
        if agent == "opencode":
            await self._run_opencode_review(
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
        agent = self._effective_agent(record)
        if not self._agent_supports_capability(agent, "review"):
            supported = ", ".join(self._agents_supporting_capability("review"))
            await self._send_message(
                message.chat_id,
                (
                    f"{self._agent_display_name(agent)} does not support /review in Telegram."
                    + (
                        f" Switch to an agent with review support: {supported}."
                        if supported
                        else ""
                    )
                ),
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
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
                        "requester_user_id": (
                            str(message.from_user_id)
                            if message.from_user_id is not None
                            else None
                        ),
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
        state = ReviewCommitSelectionState(
            items=items,
            delivery=delivery,
            requester_user_id=(
                str(message.from_user_id) if message.from_user_id is not None else None
            ),
        )
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
        except (httpx.HTTPError, ConnectionError, OSError) as exc:
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
