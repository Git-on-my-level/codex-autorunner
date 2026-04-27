from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, cast

from ..logging_utils import log_event
from .interfaces import (
    FreshConversationRequiredError,
    RuntimeThreadHarness,
    ThreadExecutionStore,
)
from .models import (
    ExecutionRecord,
    MessageRequest,
    QueuedExecutionRequest,
    ThreadTarget,
)
from .runtime_bindings import RuntimeThreadBinding, get_runtime_thread_binding
from .transcript_mirror import TranscriptMirrorStore

_MISSING_THREAD_MARKERS = (
    "missing thread",
    "thread not found",
    "no rollout found for thread id",
    "unknown hermes turn",
    "no active hermes turn tracked",
)
_RECOVERABLE_BACKEND_MARKERS = _MISSING_THREAD_MARKERS + ("event loop is closed",)
_REHYDRATION_TRANSCRIPT_LIMIT = 3
_REHYDRATION_TEXT_LIMIT = 4_000
_FRESH_BACKEND_SESSION_NOTICE = (
    "Notice: I started a new live session for this conversation."
)
_FRESH_BACKEND_SESSION_REHYDRATED_NOTICE = (
    "Notice: I started a new live session for this conversation and recovered "
    "context from durable history."
)
CLAIMED_EXECUTION_START_CANCELLED_ERROR = (
    "Runtime thread start cancelled before completion"
)

logger = logging.getLogger(__name__)


def _is_missing_thread_error(exc: Exception) -> bool:
    return any(marker in str(exc).lower() for marker in _MISSING_THREAD_MARKERS)


def _is_recoverable_backend_error(exc: Exception) -> bool:
    return any(marker in str(exc).lower() for marker in _RECOVERABLE_BACKEND_MARKERS)


async def _resolve_harness_runtime_instance_id(
    harness: RuntimeThreadHarness, workspace_root: Path
) -> Optional[str]:
    resolver = getattr(harness, "backend_runtime_instance_id", None)
    if not callable(resolver):
        return None
    try:
        runtime_instance_id = await resolver(workspace_root)
    except (AttributeError, TypeError, RuntimeError, OSError, ValueError):
        logger.debug(
            "Failed to resolve backend runtime instance id",
            exc_info=True,
        )
        return None
    if not isinstance(runtime_instance_id, str):
        return None
    normalized = runtime_instance_id.strip()
    return normalized or None


def _resolve_thread_runtime_binding(
    thread_store: ThreadExecutionStore, thread_target_id: str
) -> Optional[RuntimeThreadBinding]:
    getter = getattr(thread_store, "get_thread_runtime_binding", None)
    if callable(getter):
        binding = getter(thread_target_id)
        if binding is not None:
            return cast(RuntimeThreadBinding, binding)
    hub_root = getattr(thread_store, "hub_root", None)
    if isinstance(hub_root, Path):
        return get_runtime_thread_binding(hub_root, thread_target_id)
    return None


def _truncate_rehydration_text(value: str, limit: int = _REHYDRATION_TEXT_LIMIT) -> str:
    stripped = value.strip()
    if len(stripped) <= limit:
        return stripped
    if limit <= 3:
        return stripped[:limit]
    return stripped[: limit - 3] + "..."


@dataclass(frozen=True)
class _ClaimedThreadExecutionRequest:
    """Typed queued execution context carried through queue replay."""

    thread: ThreadTarget
    execution: ExecutionRecord
    queued_request: QueuedExecutionRequest

    @property
    def request(self) -> MessageRequest:
        return self.queued_request.request

    @property
    def client_request_id(self) -> Optional[str]:
        return self.queued_request.client_request_id

    @property
    def sandbox_policy(self) -> Optional[Any]:
        return self.queued_request.sandbox_policy

    def as_legacy_tuple(
        self,
    ) -> tuple[ThreadTarget, ExecutionRecord, MessageRequest, Optional[str], Any]:
        return (
            self.thread,
            self.execution,
            self.request,
            self.client_request_id,
            self.sandbox_policy,
        )


@dataclass
class _ThreadExecutionLifecycle:
    """Owns runtime-thread start and queued replay lifecycle concerns.

    Ownership contract:
    - This class is responsible for starting new executions and replaying
      queued executions.  It handles harness preparation, conversation
      creation/resumption, rehydration prefix assembly, and fresh-conversation
      retries.
    - It must **not** own recovery or completion-gap logic. Stale-runtime
      mismatch hints for resume are delegated to ``_ThreadRecoveryHelper``
      (logging only; binding clearing stays in proven-failure paths here).
    - It never records terminal execution results directly; that responsibility
      belongs to the thread store and recovery helper.
    """

    thread_store: ThreadExecutionStore
    get_execution: Callable[[str, str], Optional[ExecutionRecord]]
    harness_for_thread: Callable[[ThreadTarget], RuntimeThreadHarness]
    _stale_binding_checker: Optional[Callable[..., bool]] = None
    _logger: Optional[logging.Logger] = None

    @property
    def _log(self) -> logging.Logger:
        return self._logger or logger

    @staticmethod
    def resolve_runtime_prompt(request: MessageRequest) -> str:
        runtime_prompt = request.message_text
        raw_runtime_prompt = request.metadata.get("runtime_prompt")
        if isinstance(raw_runtime_prompt, str) and raw_runtime_prompt.strip():
            runtime_prompt = raw_runtime_prompt
        return runtime_prompt

    @staticmethod
    def resolve_existing_session_runtime_prompt(
        request: MessageRequest,
    ) -> Optional[str]:
        raw_runtime_prompt = request.metadata.get("existing_session_runtime_prompt")
        if isinstance(raw_runtime_prompt, str) and raw_runtime_prompt.strip():
            return raw_runtime_prompt
        return None

    def build_rehydration_prefix(
        self, thread: ThreadTarget, *, include_compact_seed: bool
    ) -> Optional[str]:
        sections: list[str] = []
        compact_seed = _truncate_rehydration_text(thread.compact_seed or "")
        if include_compact_seed and compact_seed:
            sections.append(f"Compacted context summary:\n{compact_seed}")

        hub_root = getattr(self.thread_store, "hub_root", None)
        if isinstance(hub_root, Path):
            transcript_store = TranscriptMirrorStore(hub_root)
            transcript_entries = transcript_store.list_target_history(
                target_kind="thread_target",
                target_id=thread.thread_target_id,
                limit=_REHYDRATION_TRANSCRIPT_LIMIT,
            )
            transcript_sections: list[str] = []
            for index, entry in enumerate(reversed(transcript_entries), start=1):
                content = _truncate_rehydration_text(str(entry.get("content") or ""))
                if not content:
                    continue
                transcript_sections.append(f"Recent transcript {index}:\n{content}")
            if transcript_sections:
                sections.append("\n\n".join(transcript_sections))

        if not sections:
            return None
        return (
            "Recovered durable conversation state for this managed thread. "
            "A fresh backend conversation was started because no live backend "
            "binding was available.\n\n" + "\n\n".join(sections)
        )

    def rehydrated_runtime_prompt(
        self, thread: ThreadTarget, runtime_prompt: str
    ) -> str:
        prefix = self.build_rehydration_prefix(
            thread,
            include_compact_seed="Context summary (from compaction):"
            not in runtime_prompt,
        )
        if not prefix:
            return runtime_prompt
        return f"{prefix}\n\n{runtime_prompt}"

    @staticmethod
    def mark_fresh_backend_session(
        request: MessageRequest,
        *,
        reason: str,
        rehydrated: bool,
    ) -> None:
        request.metadata["fresh_backend_session_started"] = True
        request.metadata["fresh_backend_session_reason"] = reason
        request.metadata["fresh_backend_session_notice"] = (
            _FRESH_BACKEND_SESSION_REHYDRATED_NOTICE
            if rehydrated
            else _FRESH_BACKEND_SESSION_NOTICE
        )

    async def start_execution(
        self,
        thread: ThreadTarget,
        request: MessageRequest,
        execution: ExecutionRecord,
        *,
        harness: RuntimeThreadHarness,
        workspace_root: Path,
        sandbox_policy: Optional[Any],
    ) -> ExecutionRecord:
        new_session_runtime_prompt = self.resolve_runtime_prompt(request)
        existing_session_runtime_prompt = (
            self.resolve_existing_session_runtime_prompt(request)
            or new_session_runtime_prompt
        )
        fresh_conversation_retry_attempted = False
        rehydrated_runtime_prompt = False
        fresh_backend_session_reason: Optional[str] = None
        previous_backend_thread_id: Optional[str] = None
        runtime_instance_id: Optional[str] = None
        conversation_id: Optional[str] = None
        used_existing_conversation = False
        try:
            await harness.ensure_ready(workspace_root)
            runtime_instance_id = await _resolve_harness_runtime_instance_id(
                harness, workspace_root
            )
            runtime_binding = _resolve_thread_runtime_binding(
                self.thread_store, thread.thread_target_id
            )
            conversation_id = (
                runtime_binding.backend_thread_id
                if runtime_binding is not None
                else None
            )
            if self._stale_binding_checker is not None:
                self._stale_binding_checker(
                    thread_target_id=thread.thread_target_id,
                    backend_thread_id=conversation_id,
                    runtime_instance_id=runtime_instance_id,
                )
            while True:
                used_existing_conversation = conversation_id is not None
                attempt_runtime_prompt = (
                    existing_session_runtime_prompt
                    if used_existing_conversation
                    else new_session_runtime_prompt
                )
                try:
                    if conversation_id:
                        try:
                            conversation = await harness.resume_conversation(
                                workspace_root, conversation_id
                            )
                        except (
                            RuntimeError,
                            OSError,
                            ValueError,
                            TypeError,
                            AttributeError,
                            ConnectionError,
                        ) as exc:
                            if not _is_recoverable_backend_error(exc):
                                raise
                            log_event(
                                self._log,
                                logging.INFO,
                                "orchestration.thread.resume_recoverable_backend_error",
                                exc=exc,
                                thread_target_id=thread.thread_target_id,
                                backend_thread_id=conversation_id,
                                action="start_new_conversation",
                            )
                            fresh_backend_session_reason = "resume_recoverable_error"
                            previous_backend_thread_id = conversation_id
                            self.thread_store.set_thread_backend_id(
                                thread.thread_target_id,
                                None,
                                backend_runtime_instance_id=None,
                            )
                            conversation_id = None
                            continue
                        resumed_conversation_id = getattr(conversation, "id", None)
                        if (
                            isinstance(resumed_conversation_id, str)
                            and resumed_conversation_id
                            and resumed_conversation_id != conversation_id
                        ):
                            conversation_id = resumed_conversation_id
                            self.thread_store.set_thread_backend_id(
                                thread.thread_target_id,
                                conversation_id,
                                backend_runtime_instance_id=runtime_instance_id,
                            )
                        elif (
                            runtime_instance_id
                            and runtime_binding
                            and runtime_binding.backend_runtime_instance_id
                            != runtime_instance_id
                        ):
                            self.thread_store.set_thread_backend_id(
                                thread.thread_target_id,
                                conversation_id,
                                backend_runtime_instance_id=runtime_instance_id,
                            )
                    else:
                        if not rehydrated_runtime_prompt:
                            prefix = self.build_rehydration_prefix(
                                thread,
                                include_compact_seed="Context summary (from compaction):"
                                not in new_session_runtime_prompt,
                            )
                            should_mark_fresh_backend_session = bool(
                                previous_backend_thread_id
                                or str(
                                    getattr(thread, "last_execution_id", "") or ""
                                ).strip()
                                or str(
                                    getattr(thread, "compact_seed", "") or ""
                                ).strip()
                            )
                            if should_mark_fresh_backend_session:
                                fresh_backend_session_reason = (
                                    fresh_backend_session_reason
                                    or "missing_backend_binding"
                                )
                                self.mark_fresh_backend_session(
                                    request,
                                    reason=fresh_backend_session_reason,
                                    rehydrated=bool(prefix),
                                )
                                log_event(
                                    self._log,
                                    logging.INFO,
                                    "orchestration.thread.fresh_backend_session_started",
                                    thread_target_id=thread.thread_target_id,
                                    execution_id=execution.execution_id,
                                    previous_backend_thread_id=(
                                        previous_backend_thread_id
                                    ),
                                    request_kind=request.kind,
                                    reason=fresh_backend_session_reason,
                                    rehydrated=bool(prefix),
                                )
                            if prefix:
                                attempt_runtime_prompt = (
                                    f"{prefix}\n\n{new_session_runtime_prompt}"
                                )
                            rehydrated_runtime_prompt = True
                        conversation = await harness.new_conversation(
                            workspace_root,
                            title=thread.display_name,
                        )
                        conversation_id = conversation.id
                        self.thread_store.set_thread_backend_id(
                            thread.thread_target_id,
                            conversation_id,
                            backend_runtime_instance_id=runtime_instance_id,
                        )
                    provisional_turn_id = f"{conversation_id}:{int(time.time() * 1000)}"
                    self.thread_store.set_execution_backend_id(
                        execution.execution_id,
                        provisional_turn_id,
                        confirmed_start=False,
                    )
                    log_event(
                        self._log,
                        logging.INFO,
                        "orchestration.thread.provisional_backend_turn_id",
                        thread_target_id=thread.thread_target_id,
                        execution_id=execution.execution_id,
                        conversation_id=conversation_id,
                        provisional_turn_id=provisional_turn_id,
                    )
                    if request.kind == "review":
                        if not harness.supports("review"):
                            raise RuntimeError(
                                f"Agent '{thread.agent_id}' does not support review mode"
                            )
                        turn = await harness.start_review(
                            workspace_root,
                            conversation_id,
                            attempt_runtime_prompt,
                            request.model,
                            request.reasoning,
                            approval_mode=request.approval_mode,
                            sandbox_policy=sandbox_policy,
                        )
                    else:
                        turn = await harness.start_turn(
                            workspace_root,
                            conversation_id,
                            attempt_runtime_prompt,
                            request.model,
                            request.reasoning,
                            approval_mode=request.approval_mode,
                            sandbox_policy=sandbox_policy,
                            input_items=request.input_items,
                        )
                    resolved_turn_id = str(getattr(turn, "turn_id", "") or "").strip()
                    if not resolved_turn_id:
                        raise RuntimeError(
                            f"Agent '{thread.agent_id}' returned an empty turn id"
                        )
                    break
                except FreshConversationRequiredError as exc:
                    if (
                        not used_existing_conversation
                        or fresh_conversation_retry_attempted
                    ):
                        raise
                    fresh_conversation_retry_attempted = True
                    log_event(
                        self._log,
                        logging.INFO,
                        "orchestration.thread.refreshing_backend_binding",
                        thread_target_id=thread.thread_target_id,
                        execution_id=execution.execution_id,
                        backend_thread_id=conversation_id,
                        operation=exc.operation,
                        status_code=exc.status_code,
                        reason=str(exc),
                    )
                    fresh_backend_session_reason = "fresh_conversation_required"
                    previous_backend_thread_id = conversation_id
                    self.thread_store.set_thread_backend_id(
                        thread.thread_target_id,
                        None,
                        backend_runtime_instance_id=None,
                    )
                    conversation_id = None
                    continue
                except (
                    RuntimeError,
                    OSError,
                    ValueError,
                    TypeError,
                    AttributeError,
                    ConnectionError,
                ) as exc:
                    if (
                        not used_existing_conversation
                        or fresh_conversation_retry_attempted
                        or not _is_recoverable_backend_error(exc)
                    ):
                        raise
                    fresh_conversation_retry_attempted = True
                    log_event(
                        self._log,
                        logging.INFO,
                        "orchestration.thread.refreshing_backend_binding",
                        thread_target_id=thread.thread_target_id,
                        execution_id=execution.execution_id,
                        backend_thread_id=conversation_id,
                        operation=(
                            "start_review" if request.kind == "review" else "start_turn"
                        ),
                        status_code=None,
                        reason=str(exc),
                    )
                    fresh_backend_session_reason = "start_turn_recoverable_error"
                    previous_backend_thread_id = conversation_id
                    self.thread_store.set_thread_backend_id(
                        thread.thread_target_id,
                        None,
                        backend_runtime_instance_id=None,
                    )
                    conversation_id = None
                    continue
        except asyncio.CancelledError as exc:
            detail = (
                str(request.metadata.get("execution_error_message") or "").strip()
                or CLAIMED_EXECUTION_START_CANCELLED_ERROR
            )
            log_event(
                self._log,
                logging.WARNING,
                "orchestration.thread.start_failed",
                thread_target_id=thread.thread_target_id,
                execution_id=execution.execution_id,
                backend_thread_id=conversation_id,
                request_kind=request.kind,
                fresh_conversation_retry_attempted=fresh_conversation_retry_attempted,
                reported_error=detail,
                error_type=type(exc).__name__,
            )
            try:
                self.thread_store.record_execution_result(
                    thread.thread_target_id,
                    execution.execution_id,
                    status="error",
                    assistant_text="",
                    error=detail,
                    backend_turn_id=None,
                    transcript_turn_id=None,
                )
            except KeyError:
                refreshed = self.get_execution(
                    thread.thread_target_id, execution.execution_id
                )
                if refreshed is None:
                    raise
            raise
        except Exception as exc:
            detail = (
                str(request.metadata.get("execution_error_message") or "").strip()
                or str(exc).strip()
                or "Runtime thread execution failed"
            )
            runtime_binding = _resolve_thread_runtime_binding(
                self.thread_store, thread.thread_target_id
            )
            log_event(
                self._log,
                logging.WARNING,
                "orchestration.thread.start_failed",
                exc=exc,
                thread_target_id=thread.thread_target_id,
                execution_id=execution.execution_id,
                backend_thread_id=(
                    runtime_binding.backend_thread_id if runtime_binding else None
                ),
                request_kind=request.kind,
                fresh_conversation_retry_attempted=fresh_conversation_retry_attempted,
                reported_error=detail,
            )
            try:
                return self.thread_store.record_execution_result(
                    thread.thread_target_id,
                    execution.execution_id,
                    status="error",
                    assistant_text="",
                    error=detail,
                    backend_turn_id=None,
                    transcript_turn_id=None,
                )
            except KeyError:
                refreshed = self.get_execution(
                    thread.thread_target_id, execution.execution_id
                )
                if refreshed is not None:
                    return refreshed
                raise

        resolved_conversation_id = getattr(turn, "conversation_id", conversation_id)
        if (
            isinstance(resolved_conversation_id, str)
            and resolved_conversation_id
            and resolved_conversation_id != conversation_id
        ):
            self.thread_store.set_thread_backend_id(
                thread.thread_target_id,
                resolved_conversation_id,
                backend_runtime_instance_id=runtime_instance_id,
            )
        self.thread_store.set_execution_backend_id(
            execution.execution_id,
            resolved_turn_id,
            confirmed_start=True,
        )
        log_event(
            self._log,
            logging.INFO,
            "orchestration.thread.runtime_turn_started",
            thread_target_id=thread.thread_target_id,
            execution_id=execution.execution_id,
            backend_thread_id=resolved_conversation_id,
            backend_turn_id=resolved_turn_id,
            request_kind=request.kind,
            reused_conversation=used_existing_conversation,
            stale_session_recovery=fresh_conversation_retry_attempted,
        )
        refreshed = self.get_execution(thread.thread_target_id, execution.execution_id)
        if refreshed is None:
            raise KeyError(
                f"Execution '{execution.execution_id}' is missing after creation"
            )
        return refreshed

    def claimed_execution_start_error_detail(
        self,
        request: MessageRequest,
        exc: BaseException,
    ) -> str:
        configured = str(
            getattr(request, "metadata", {}).get("execution_error_message") or ""
        ).strip()
        if configured:
            return configured
        detail = str(exc).strip()
        if detail:
            return detail
        if isinstance(exc, asyncio.CancelledError):
            return CLAIMED_EXECUTION_START_CANCELLED_ERROR
        return "Runtime thread execution failed"

    def record_claimed_execution_start_failure(
        self,
        claimed: _ClaimedThreadExecutionRequest,
        exc: BaseException,
    ) -> None:
        try:
            current = self.get_execution(
                claimed.thread.thread_target_id, claimed.execution.execution_id
            )
        except Exception:
            current = None
        if current is not None and current.status != "running":
            return
        detail = self.claimed_execution_start_error_detail(claimed.request, exc)
        logged_exc = exc if isinstance(exc, Exception) else None
        log_event(
            self._log,
            logging.WARNING,
            "orchestration.thread.claimed_start_failed",
            exc=logged_exc,
            thread_target_id=claimed.thread.thread_target_id,
            execution_id=claimed.execution.execution_id,
            request_kind=claimed.request.kind,
            reported_error=detail,
        )
        try:
            self.thread_store.record_execution_result(
                claimed.thread.thread_target_id,
                claimed.execution.execution_id,
                status="error",
                assistant_text="",
                error=detail,
                backend_turn_id=None,
                transcript_turn_id=None,
            )
        except KeyError:
            return

    async def start_claimed_execution_request(
        self,
        claimed: _ClaimedThreadExecutionRequest,
        *,
        harness: Optional[RuntimeThreadHarness] = None,
        workspace_root: Optional[Path] = None,
    ) -> tuple[ExecutionRecord, RuntimeThreadHarness]:
        resolved_workspace_root = workspace_root
        if resolved_workspace_root is None:
            if not claimed.thread.workspace_root:
                raise RuntimeError("Thread target is missing workspace_root")
            resolved_workspace_root = Path(claimed.thread.workspace_root)
        try:
            resolved_harness = harness or self.harness_for_thread(claimed.thread)
            started = await self.start_execution(
                claimed.thread,
                claimed.request,
                claimed.execution,
                harness=resolved_harness,
                workspace_root=resolved_workspace_root,
                sandbox_policy=claimed.sandbox_policy,
            )
            return started, resolved_harness
        except BaseException as exc:
            self.record_claimed_execution_start_failure(claimed, exc)
            raise
