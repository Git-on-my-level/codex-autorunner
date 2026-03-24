from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import secrets
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, cast

import httpx

from .....agents.base import (
    harness_progress_event_stream,
    harness_supports_progress_event_stream,
)
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
from .....agents.registry import get_registered_agents
from .....core.context_awareness import (
    has_file_context_signal,
    maybe_inject_car_awareness,
    maybe_inject_filebox_hint,
    maybe_inject_prompt_writing_hint,
)
from .....core.injected_context import wrap_injected_context
from .....core.logging_utils import log_event
from .....core.orchestration import (
    MessageRequest,
    build_harness_backed_orchestration_service,
)
from .....core.orchestration.runtime_thread_events import (
    RuntimeThreadRunEventState,
    normalize_runtime_thread_raw_event,
    recover_post_completion_outcome,
    terminal_run_event_from_outcome,
)
from .....core.orchestration.runtime_threads import (
    RuntimeThreadExecution,
    RuntimeThreadOutcome,
    await_runtime_thread_outcome,
    begin_next_queued_runtime_thread_execution,
    begin_runtime_thread_execution,
)
from .....core.pma_context import (
    build_hub_snapshot,
    format_pma_discoverability_preamble,
    format_pma_prompt,
    load_pma_prompt,
)
from .....core.pma_thread_store import PmaThreadStore
from .....core.pma_transcripts import PmaTranscriptStore
from .....core.state import now_iso
from .....core.utils import canonicalize_path
from .....integrations.app_server.threads import (
    AppServerThreadRegistry,
    pma_base_key,
    pma_topic_scoped_key,
)
from .....integrations.chat.compaction import match_pending_compact_seed
from .....integrations.chat.runtime_thread_errors import (
    resolve_runtime_thread_error_detail as _resolve_runtime_thread_result_error_detail,
)
from .....integrations.chat.runtime_thread_errors import (
    sanitize_runtime_thread_error as _sanitize_runtime_thread_result_error,
)
from .....integrations.github.context_injection import maybe_inject_github_context
from ....app_server.client import (
    CodexAppServerClient,
    CodexAppServerDisconnected,
    CodexAppServerResponseError,
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
    PLACEHOLDER_TEXT,
    QUEUED_PLACEHOLDER_TEXT,
    RESUME_PREVIEW_ASSISTANT_LIMIT,
    RESUME_PREVIEW_USER_LIMIT,
    SHELL_MESSAGE_BUFFER_CHARS,
    TELEGRAM_MAX_MESSAGE_LENGTH,
    WHISPER_TRANSCRIPT_DISCLAIMER,
    TurnKey,
)
from ...forwarding import format_forwarded_telegram_message_text
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
    _set_thread_summary,
    _with_conversation_id,
    format_public_error,
    is_interrupt_status,
)
from ...state import topic_key as build_topic_key
from ..utils import (
    _build_opencode_token_usage,
)

if TYPE_CHECKING:
    from ...state import TelegramTopicRecord

from .command_utils import (
    _format_httpx_exception,
    _format_opencode_exception,
)
from .shared import SharedHelpers

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

TELEGRAM_PMA_PUBLIC_EXECUTION_ERROR = "Telegram PMA turn failed"
TELEGRAM_REPO_PUBLIC_EXECUTION_ERROR = "Telegram turn failed"
TELEGRAM_PMA_TIMEOUT_ERROR = "Telegram PMA turn timed out"
TELEGRAM_REPO_TIMEOUT_ERROR = "Telegram turn timed out"
TELEGRAM_PMA_INTERRUPTED_ERROR = "Telegram PMA turn interrupted"
TELEGRAM_REPO_INTERRUPTED_ERROR = "Telegram turn interrupted"
TELEGRAM_PMA_TIMEOUT_SECONDS = 7200


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
    intermediate_response: str = ""
    interrupt_status_turn_id: Optional[str] = None
    interrupt_status_fallback_text: Optional[str] = None


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
    return format_public_error(detail, limit=limit)


def _format_telegram_download_error(exc: Exception) -> Optional[str]:
    for current in _iter_exception_chain(exc):
        if isinstance(current, Exception):
            detail = _format_httpx_exception(current)
            if detail:
                return format_public_error(detail)
            message = str(current).strip()
            if message and message not in _GENERIC_TELEGRAM_ERRORS:
                return format_public_error(message)
    return None


def _format_download_failure_response(kind: str, detail: Optional[str]) -> str:
    base = f"Failed to download {kind}."
    if detail:
        return f"{base} Reason: {detail}"
    return base


def _build_managed_thread_input_items(
    runtime_prompt: str,
    input_items: Optional[list[dict[str, Any]]],
) -> Optional[list[dict[str, Any]]]:
    if not input_items:
        return None
    normalized: list[dict[str, Any]] = []
    replaced_text = False
    for item in input_items:
        if not isinstance(item, dict):
            continue
        item_copy = dict(item)
        if not replaced_text and str(item_copy.get("type") or "").strip() == "text":
            item_copy["text"] = runtime_prompt
            replaced_text = True
        normalized.append(item_copy)
    if not replaced_text:
        normalized.insert(0, {"type": "text", "text": runtime_prompt})
    return normalized or None


def _build_telegram_thread_orchestration_service(handlers: Any) -> Any:
    cached = getattr(handlers, "_telegram_managed_thread_orchestration_service", None)
    if cached is None:
        cached = getattr(handlers, "_telegram_thread_orchestration_service", None)
    if cached is not None:
        return cached

    descriptors = get_registered_agents()

    def _make_harness(agent_id: str) -> Any:
        descriptor = descriptors.get(agent_id)
        if descriptor is None:
            raise KeyError(f"Unknown agent definition '{agent_id}'")
        return descriptor.make_harness(handlers)

    state_root = getattr(getattr(handlers, "_config", None), "root", None)
    if state_root is None:
        state_root = getattr(handlers, "_hub_root", None)
    if state_root is None:
        state_root = Path(".")

    created = build_harness_backed_orchestration_service(
        descriptors=descriptors,
        harness_factory=_make_harness,
        pma_thread_store=PmaThreadStore(state_root),
    )
    handlers._telegram_managed_thread_orchestration_service = created
    handlers._telegram_thread_orchestration_service = created
    return created


def _get_telegram_thread_binding(
    handlers: Any,
    *,
    surface_key: str,
    mode: Optional[str] = None,
) -> tuple[Any, Any, Any]:
    orchestration_service = _build_telegram_thread_orchestration_service(handlers)
    binding = orchestration_service.get_binding(
        surface_kind="telegram",
        surface_key=surface_key,
    )
    normalized_mode = (
        mode.strip().lower() if isinstance(mode, str) and mode.strip() else None
    )
    if binding is None:
        return orchestration_service, None, None
    if normalized_mode is not None:
        binding_mode = str(getattr(binding, "mode", "") or "").strip().lower()
        if binding_mode != normalized_mode:
            return orchestration_service, binding, None
    thread = orchestration_service.get_thread_target(binding.thread_target_id)
    return orchestration_service, binding, thread


def _get_thread_runtime_binding(
    orchestration_service: Any, thread_target_id: str
) -> Any:
    getter = getattr(orchestration_service, "get_thread_runtime_binding", None)
    if not callable(getter) or not thread_target_id:
        return None
    try:
        return getter(thread_target_id)
    except Exception:
        return None


async def _resolve_telegram_managed_thread(
    handlers: Any,
    *,
    surface_key: str,
    workspace_root: Path,
    agent: str,
    repo_id: Optional[str],
    resource_kind: Optional[str] = None,
    resource_id: Optional[str] = None,
    mode: str = "pma",
    pma_enabled: bool = True,
    backend_thread_id: Optional[str] = None,
    allow_new_thread: bool = True,
) -> Any:
    orchestration_service, binding, thread = _get_telegram_thread_binding(
        handlers,
        surface_key=surface_key,
        mode=mode,
    )
    canonical_workspace = str(workspace_root.resolve())
    normalized_backend_thread_id = (
        str(backend_thread_id).strip()
        if isinstance(backend_thread_id, str) and backend_thread_id.strip()
        else None
    )
    existing_backend_thread_id = (
        str(getattr(thread, "backend_thread_id", None) or "").strip() or None
    )
    backend_runtime_instance_id: Optional[str] = None
    if pma_enabled:
        normalized_backend_thread_id = None
    if normalized_backend_thread_id:
        backend_runtime_instance_id = (
            await orchestration_service.resolve_backend_runtime_instance_id(
                agent,
                workspace_root,
            )
        )
        if backend_runtime_instance_id is None:
            log_event(
                handlers._logger,
                logging.INFO,
                "telegram.thread.binding.runtime_unavailable",
                surface_key=surface_key,
                backend_thread_id=normalized_backend_thread_id,
                agent=agent,
                workspace_root=str(workspace_root),
                mode=mode,
            )
            if (
                existing_backend_thread_id is not None
                and existing_backend_thread_id != normalized_backend_thread_id
            ):
                log_event(
                    handlers._logger,
                    logging.INFO,
                    "telegram.thread.binding.rebind_rejected",
                    surface_key=surface_key,
                    existing_backend_thread_id=existing_backend_thread_id,
                    requested_backend_thread_id=normalized_backend_thread_id,
                    agent=agent,
                    workspace_root=str(workspace_root),
                    mode=mode,
                    reason="runtime_unavailable",
                )
    reusable_thread = (
        thread is not None
        and thread.agent_id == agent
        and str(thread.workspace_root or "").strip() == canonical_workspace
    )
    effective_backend_thread_id = normalized_backend_thread_id
    if (
        reusable_thread
        and normalized_backend_thread_id is not None
        and backend_runtime_instance_id is None
        and existing_backend_thread_id is not None
    ):
        effective_backend_thread_id = existing_backend_thread_id
    if reusable_thread and (
        str(thread.lifecycle_status or "").strip().lower() != "active"
        or thread.backend_thread_id != effective_backend_thread_id
    ):
        thread = orchestration_service.resume_thread_target(
            thread.thread_target_id,
            backend_thread_id=effective_backend_thread_id,
            backend_runtime_instance_id=backend_runtime_instance_id,
        )
    elif not reusable_thread:
        if not allow_new_thread and not effective_backend_thread_id:
            return orchestration_service, None
        thread = orchestration_service.create_thread_target(
            agent,
            workspace_root,
            repo_id=repo_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
            display_name=f"telegram:{surface_key}",
            backend_thread_id=effective_backend_thread_id,
            metadata=(
                {
                    "backend_runtime_instance_id": backend_runtime_instance_id,
                }
                if backend_runtime_instance_id is not None
                else None
            ),
        )
    orchestration_service.upsert_binding(
        surface_kind="telegram",
        surface_key=surface_key,
        thread_target_id=thread.thread_target_id,
        agent_id=agent,
        repo_id=repo_id,
        resource_kind=resource_kind,
        resource_id=resource_id,
        mode=mode,
        metadata={
            "topic_key": surface_key,
            "pma_enabled": pma_enabled,
            "surface_key": surface_key,
        },
    )
    return orchestration_service, thread


async def _reset_telegram_thread_binding(
    handlers: Any,
    *,
    surface_key: str,
    workspace_root: Path,
    agent: str,
    repo_id: Optional[str],
    resource_kind: Optional[str],
    resource_id: Optional[str],
    mode: str,
    pma_enabled: bool,
) -> tuple[bool, str]:
    orchestration_service, _binding, current_thread = _get_telegram_thread_binding(
        handlers,
        surface_key=surface_key,
        mode=mode,
    )
    had_previous = current_thread is not None
    normalized_repo_id = repo_id.strip() if isinstance(repo_id, str) else None
    if current_thread is not None:
        stop_outcome = await orchestration_service.stop_thread(
            current_thread.thread_target_id
        )
        if stop_outcome.recovered_lost_backend:
            log_event(
                handlers._logger,
                logging.INFO,
                "telegram.thread.recovered_lost_backend",
                surface_key=surface_key,
                managed_thread_id=current_thread.thread_target_id,
                mode=mode,
            )
        orchestration_service.archive_thread_target(current_thread.thread_target_id)
    replacement = orchestration_service.create_thread_target(
        agent,
        workspace_root,
        repo_id=normalized_repo_id or None,
        resource_kind=resource_kind,
        resource_id=resource_id,
        display_name=f"telegram:{surface_key}",
    )
    orchestration_service.upsert_binding(
        surface_kind="telegram",
        surface_key=surface_key,
        thread_target_id=replacement.thread_target_id,
        agent_id=agent,
        repo_id=normalized_repo_id or None,
        resource_kind=resource_kind,
        resource_id=resource_id,
        mode=mode,
        metadata={
            "topic_key": surface_key,
            "pma_enabled": pma_enabled,
            "surface_key": surface_key,
        },
    )
    return had_previous, replacement.thread_target_id


async def _sync_telegram_thread_binding(
    handlers: Any,
    *,
    surface_key: str,
    workspace_root: Path,
    agent: str,
    repo_id: Optional[str],
    resource_kind: Optional[str],
    resource_id: Optional[str],
    backend_thread_id: Optional[str],
    mode: str,
    pma_enabled: bool,
    replace_existing: bool = False,
) -> Any:
    orchestration_service, _binding, current_thread = _get_telegram_thread_binding(
        handlers,
        surface_key=surface_key,
        mode=mode,
    )
    canonical_workspace = str(workspace_root.resolve())
    normalized_repo_id = repo_id.strip() if isinstance(repo_id, str) else None
    effective_backend_thread_id = None if pma_enabled else backend_thread_id
    existing_backend_thread_id = (
        str(getattr(current_thread, "backend_thread_id", None) or "").strip() or None
    )
    backend_runtime_instance_id: Optional[str] = None
    if effective_backend_thread_id is not None:
        backend_runtime_instance_id = (
            await orchestration_service.resolve_backend_runtime_instance_id(
                agent,
                workspace_root,
            )
        )
        if backend_runtime_instance_id is None:
            log_event(
                handlers._logger,
                logging.INFO,
                "telegram.thread.binding.runtime_unavailable",
                surface_key=surface_key,
                backend_thread_id=effective_backend_thread_id,
                agent=agent,
                workspace_root=str(workspace_root),
                mode=mode,
            )
            if (
                existing_backend_thread_id is not None
                and existing_backend_thread_id != effective_backend_thread_id
            ):
                log_event(
                    handlers._logger,
                    logging.INFO,
                    "telegram.thread.binding.rebind_rejected",
                    surface_key=surface_key,
                    existing_backend_thread_id=existing_backend_thread_id,
                    requested_backend_thread_id=effective_backend_thread_id,
                    agent=agent,
                    workspace_root=str(workspace_root),
                    mode=mode,
                    reason="runtime_unavailable",
                )
    if replace_existing and current_thread is not None:
        stop_outcome = await orchestration_service.stop_thread(
            current_thread.thread_target_id
        )
        if stop_outcome.recovered_lost_backend:
            log_event(
                handlers._logger,
                logging.INFO,
                "telegram.thread.recovered_lost_backend",
                surface_key=surface_key,
                managed_thread_id=current_thread.thread_target_id,
                mode=mode,
            )
        orchestration_service.archive_thread_target(current_thread.thread_target_id)
        current_thread = None
    if (
        current_thread is not None
        and current_thread.agent_id == agent
        and str(current_thread.workspace_root or "").strip() == canonical_workspace
    ):
        if (
            effective_backend_thread_id is not None
            and backend_runtime_instance_id is None
            and existing_backend_thread_id is not None
        ):
            effective_backend_thread_id = existing_backend_thread_id
        current_thread = orchestration_service.resume_thread_target(
            current_thread.thread_target_id,
            backend_thread_id=effective_backend_thread_id,
            backend_runtime_instance_id=backend_runtime_instance_id,
        )
    else:
        current_thread = orchestration_service.create_thread_target(
            agent,
            workspace_root,
            repo_id=normalized_repo_id or None,
            resource_kind=resource_kind,
            resource_id=resource_id,
            display_name=f"telegram:{surface_key}",
            backend_thread_id=effective_backend_thread_id,
            metadata=(
                {"backend_runtime_instance_id": backend_runtime_instance_id}
                if backend_runtime_instance_id is not None
                else None
            ),
        )
    orchestration_service.upsert_binding(
        surface_kind="telegram",
        surface_key=surface_key,
        thread_target_id=current_thread.thread_target_id,
        agent_id=agent,
        repo_id=normalized_repo_id or None,
        resource_kind=resource_kind,
        resource_id=resource_id,
        mode=mode,
        metadata={
            "topic_key": surface_key,
            "pma_enabled": pma_enabled,
            "surface_key": surface_key,
        },
    )
    return orchestration_service, current_thread


async def _finalize_telegram_managed_thread_execution(
    handlers: Any,
    *,
    orchestration_service: Any,
    started: RuntimeThreadExecution,
    surface_key: str,
    chat_id: int,
    thread_id: Optional[int],
    public_execution_error: str = TELEGRAM_PMA_PUBLIC_EXECUTION_ERROR,
    timeout_error: str = TELEGRAM_PMA_TIMEOUT_ERROR,
    interrupted_error: str = TELEGRAM_PMA_INTERRUPTED_ERROR,
    runtime_event_state: Optional[RuntimeThreadRunEventState] = None,
    on_progress_event: Optional[Any] = None,
) -> dict[str, Any]:
    thread_store = PmaThreadStore(handlers._config.root)
    transcripts = PmaTranscriptStore(handlers._config.root)
    managed_thread_id = started.thread.thread_target_id
    managed_turn_id = started.execution.execution_id
    current_thread_row = thread_store.get_thread(managed_thread_id) or {}
    current_preview = _preview_from_text(
        str(started.request.message_text or ""),
        RESUME_PREVIEW_USER_LIMIT,
    )
    runtime_binding = _get_thread_runtime_binding(
        orchestration_service, managed_thread_id
    )
    current_backend_thread_id = str(
        getattr(runtime_binding, "backend_thread_id", None)
        or started.thread.backend_thread_id
        or ""
    ).strip()
    event_state = runtime_event_state or RuntimeThreadRunEventState()
    stream_task: Optional[asyncio.Task[None]] = None

    stream_backend_thread_id = current_backend_thread_id
    stream_backend_turn_id = str(started.execution.backend_id or "").strip()
    if not stream_backend_turn_id:
        stream_backend_turn_id = str(started.execution.execution_id or "").strip()

    if (
        harness_supports_progress_event_stream(started.harness)
        and stream_backend_thread_id
        and stream_backend_turn_id
    ):

        async def _pump_runtime_events() -> None:
            try:
                async for raw_event in harness_progress_event_stream(
                    started.harness,
                    started.workspace_root,
                    stream_backend_thread_id,
                    stream_backend_turn_id,
                ):
                    run_events = await normalize_runtime_thread_raw_event(
                        raw_event,
                        event_state,
                    )
                    if on_progress_event is None:
                        continue
                    for run_event in run_events:
                        try:
                            await on_progress_event(run_event)
                        except Exception:
                            continue
            except Exception:
                return

        stream_task = asyncio.create_task(_pump_runtime_events())

    try:
        outcome = await await_runtime_thread_outcome(
            started,
            interrupt_event=None,
            timeout_seconds=TELEGRAM_PMA_TIMEOUT_SECONDS,
            execution_error_message=public_execution_error,
        )
    except Exception:
        outcome = RuntimeThreadOutcome(
            status="error",
            assistant_text="",
            error=public_execution_error,
            backend_thread_id=current_backend_thread_id,
            backend_turn_id=started.execution.backend_id,
        )
    finally:
        if stream_task is not None:
            stream_task.cancel()
            with suppress(asyncio.CancelledError):
                await stream_task

    recovered_outcome = recover_post_completion_outcome(outcome, event_state)
    if recovered_outcome is not outcome:
        handlers._logger.warning(
            "Telegram runtime turn recovered from post-completion error: thread=%s turn=%s error=%s",
            managed_thread_id,
            managed_turn_id,
            outcome.error,
        )
        outcome = recovered_outcome

    if on_progress_event is not None:
        with suppress(Exception):
            await on_progress_event(
                terminal_run_event_from_outcome(outcome, event_state)
            )

    resolved_assistant_text = (
        outcome.assistant_text or event_state.best_assistant_text()
    )

    finalized_thread = orchestration_service.get_thread_target(managed_thread_id)
    runtime_binding = _get_thread_runtime_binding(
        orchestration_service, managed_thread_id
    )
    resolved_backend_thread_id = (
        str(
            getattr(runtime_binding, "backend_thread_id", None)
            or getattr(finalized_thread, "backend_thread_id", None)
            or ""
        ).strip()
        or outcome.backend_thread_id
        or current_backend_thread_id
    )

    if outcome.status == "ok":
        transcript_turn_id: Optional[str] = None
        transcript_metadata = {
            "managed_thread_id": managed_thread_id,
            "managed_turn_id": managed_turn_id,
            "repo_id": current_thread_row.get("repo_id"),
            "workspace_root": str(started.workspace_root),
            "agent": current_thread_row.get("agent"),
            "backend_thread_id": resolved_backend_thread_id,
            "backend_turn_id": outcome.backend_turn_id,
            "model": started.request.model,
            "reasoning": started.request.reasoning,
            "status": "ok",
            "surface_kind": "telegram",
            "surface_key": surface_key,
            "chat_id": chat_id,
            "thread_id": thread_id,
        }
        try:
            transcripts.write_transcript(
                turn_id=managed_turn_id,
                metadata=transcript_metadata,
                assistant_text=resolved_assistant_text,
            )
            transcript_turn_id = managed_turn_id
        except Exception as exc:
            handlers._logger.warning(
                "Failed to persist Telegram transcript (thread=%s turn=%s): %s",
                managed_thread_id,
                managed_turn_id,
                exc,
            )
        try:
            finalized_execution = orchestration_service.record_execution_result(
                managed_thread_id,
                managed_turn_id,
                status="ok",
                assistant_text=resolved_assistant_text,
                error=None,
                backend_turn_id=outcome.backend_turn_id,
                transcript_turn_id=transcript_turn_id,
            )
        except KeyError:
            finalized_execution = orchestration_service.get_execution(
                managed_thread_id,
                managed_turn_id,
            )
        finalized_status = str(
            getattr(finalized_execution, "status", "") if finalized_execution else ""
        ).strip()
        if finalized_status != "ok":
            detail = public_execution_error
            if finalized_status == "interrupted":
                detail = interrupted_error
            elif finalized_status == "error" and finalized_execution is not None:
                detail = _resolve_runtime_thread_result_error_detail(
                    execution_error=getattr(finalized_execution, "error", None),
                    event_error=event_state.last_error_message,
                    public_error=public_execution_error,
                    timeout_error=timeout_error,
                    interrupted_error=interrupted_error,
                )
            return {
                "status": "error",
                "assistant_text": "",
                "error": detail,
                "managed_thread_id": managed_thread_id,
                "managed_turn_id": managed_turn_id,
                "backend_thread_id": resolved_backend_thread_id,
                "token_usage": event_state.token_usage,
            }
        thread_store.update_thread_after_turn(
            managed_thread_id,
            last_turn_id=managed_turn_id,
            last_message_preview=current_preview,
        )
        return {
            "status": "ok",
            "assistant_text": resolved_assistant_text,
            "error": None,
            "managed_thread_id": managed_thread_id,
            "managed_turn_id": managed_turn_id,
            "backend_thread_id": resolved_backend_thread_id,
            "token_usage": event_state.token_usage,
        }

    if outcome.status == "interrupted":
        try:
            orchestration_service.record_execution_interrupted(
                managed_thread_id,
                managed_turn_id,
            )
        except KeyError:
            pass
        return {
            "status": "interrupted",
            "assistant_text": "",
            "error": interrupted_error,
            "managed_thread_id": managed_thread_id,
            "managed_turn_id": managed_turn_id,
            "backend_thread_id": resolved_backend_thread_id,
            "token_usage": event_state.token_usage,
        }

    detail = _resolve_runtime_thread_result_error_detail(
        outcome_error=outcome.error,
        event_error=event_state.last_error_message,
        public_error=public_execution_error,
        timeout_error=timeout_error,
        interrupted_error=interrupted_error,
    )
    try:
        orchestration_service.record_execution_result(
            managed_thread_id,
            managed_turn_id,
            status="error",
            assistant_text="",
            error=detail,
            backend_turn_id=outcome.backend_turn_id,
            transcript_turn_id=None,
        )
    except KeyError:
        pass
    return {
        "status": "error",
        "assistant_text": "",
        "error": detail,
        "managed_thread_id": managed_thread_id,
        "managed_turn_id": managed_turn_id,
        "backend_thread_id": resolved_backend_thread_id,
        "token_usage": event_state.token_usage,
    }


def _ensure_telegram_managed_thread_queue_worker(
    handlers: Any,
    *,
    orchestration_service: Any,
    managed_thread_id: str,
    surface_key: str,
    record: "TelegramTopicRecord",
    chat_id: int,
    thread_id: Optional[int],
    public_execution_error: str = TELEGRAM_PMA_PUBLIC_EXECUTION_ERROR,
    timeout_error: str = TELEGRAM_PMA_TIMEOUT_ERROR,
    interrupted_error: str = TELEGRAM_PMA_INTERRUPTED_ERROR,
) -> None:
    task_map = getattr(handlers, "_telegram_managed_thread_queue_tasks", None)
    if not isinstance(task_map, dict):
        task_map = {}
        handlers._telegram_managed_thread_queue_tasks = task_map
    existing = task_map.get(managed_thread_id)
    if isinstance(existing, asyncio.Task) and not existing.done():
        return

    worker_task: Optional[asyncio.Task[Any]] = None

    async def _run_with_telegram_typing_indicator(work: Any) -> None:
        begin = getattr(handlers, "_begin_typing_indicator", None)
        end = getattr(handlers, "_end_typing_indicator", None)
        began = False
        if callable(begin):
            try:
                await begin(chat_id, thread_id)
                began = True
            except Exception as exc:
                log_event(
                    handlers._logger,
                    logging.DEBUG,
                    "telegram.typing.begin.failed",
                    chat_id=chat_id,
                    thread_id=thread_id,
                    exc=exc,
                )
        try:
            await work()
        finally:
            if began and callable(end):
                try:
                    await end(chat_id, thread_id)
                except Exception as exc:
                    log_event(
                        handlers._logger,
                        logging.DEBUG,
                        "telegram.typing.end.failed",
                        chat_id=chat_id,
                        thread_id=thread_id,
                        exc=exc,
                    )

    async def _queue_worker() -> None:
        try:
            while True:
                if orchestration_service.get_running_execution(managed_thread_id):
                    await asyncio.sleep(0.1)
                    continue
                started = await begin_next_queued_runtime_thread_execution(
                    orchestration_service,
                    managed_thread_id,
                )
                if started is None:
                    break

                async def _process_started_execution(
                    started_execution: RuntimeThreadExecution = started,
                ) -> None:
                    finalized = await _finalize_telegram_managed_thread_execution(
                        handlers,
                        orchestration_service=orchestration_service,
                        started=started_execution,
                        surface_key=surface_key,
                        chat_id=chat_id,
                        thread_id=thread_id,
                        public_execution_error=public_execution_error,
                        timeout_error=timeout_error,
                        interrupted_error=interrupted_error,
                    )
                    if finalized["status"] == "ok":
                        message_text = str(
                            finalized.get("assistant_text") or ""
                        ).strip()
                        if message_text:
                            await handlers._send_message(
                                chat_id,
                                message_text,
                                thread_id=thread_id,
                                reply_to=None,
                            )
                        await handlers._flush_outbox_files(
                            record,
                            chat_id=chat_id,
                            thread_id=thread_id,
                            reply_to=None,
                        )
                        return
                    await handlers._send_message(
                        chat_id,
                        (
                            "Turn failed: "
                            f"{finalized.get('error') or public_execution_error}"
                        ),
                        thread_id=thread_id,
                        reply_to=None,
                    )

                await _run_with_telegram_typing_indicator(_process_started_execution)
        finally:
            if (
                worker_task is not None
                and task_map.get(managed_thread_id) is worker_task
            ):
                task_map.pop(managed_thread_id, None)

    worker_task = handlers._spawn_task(_queue_worker())
    task_map[managed_thread_id] = worker_task


def _sync_pma_registry_thread_id(
    handlers: Any,
    record: "TelegramTopicRecord",
    message: TelegramMessage,
    backend_thread_id: Optional[str],
) -> None:
    if not isinstance(backend_thread_id, str) or not backend_thread_id.strip():
        return
    registry = getattr(handlers, "_hub_thread_registry", None)
    pma_key_builder = getattr(handlers, "_pma_registry_key", None)
    if registry is None or not callable(pma_key_builder):
        return
    pma_key = pma_key_builder(record, message)
    if not isinstance(pma_key, str) or not pma_key.strip():
        return
    registry.set_thread_id(pma_key, backend_thread_id)


async def _run_telegram_managed_thread_turn(
    handlers: Any,
    *,
    message: TelegramMessage,
    runtime: Any,
    record: "TelegramTopicRecord",
    topic_key: str,
    prompt_text: str,
    input_items: Optional[list[dict[str, Any]]],
    send_placeholder: bool,
    send_failure_response: bool,
    transcript_message_id: Optional[int],
    transcript_text: Optional[str],
    placeholder_id: Optional[int],
    allow_new_thread: bool = True,
    missing_thread_message: Optional[str] = None,
    mode: str = "pma",
    pma_enabled: bool = True,
    execution_prompt: Optional[str] = None,
    public_execution_error: str = TELEGRAM_PMA_PUBLIC_EXECUTION_ERROR,
    timeout_error: str = TELEGRAM_PMA_TIMEOUT_ERROR,
    interrupted_error: str = TELEGRAM_PMA_INTERRUPTED_ERROR,
    approval_policy: Optional[str] = None,
    sandbox_policy: Optional[Any] = None,
) -> _TurnRunResult | _TurnRunFailure:
    prepared_placeholder_id = await handlers._prepare_turn_placeholder(
        message,
        placeholder_id=placeholder_id,
        send_placeholder=send_placeholder,
        queued=False,
    )
    workspace_root = canonicalize_path(Path(record.workspace_path or ""))
    agent = handlers._effective_agent(record)
    repo_id = record.repo_id.strip() if isinstance(record.repo_id, str) else None
    current_backend_thread_id = (
        str(record.active_thread_id).strip()
        if (
            not pma_enabled
            and isinstance(getattr(record, "active_thread_id", None), str)
            and str(record.active_thread_id).strip()
        )
        else None
    )
    if (
        not pma_enabled
        and not current_backend_thread_id
        and allow_new_thread
        and agent != "opencode"
    ):
        try:
            client = await handlers._client_for_workspace(record.workspace_path)
        except AppServerUnavailableError as exc:
            log_event(
                handlers._logger,
                logging.WARNING,
                "telegram.app_server.unavailable",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            failure_message = "App server unavailable; try again or check logs."
            if send_failure_response:
                await handlers._send_message(
                    message.chat_id,
                    failure_message,
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
            return _TurnRunFailure(
                failure_message,
                prepared_placeholder_id,
                transcript_message_id,
                transcript_text,
            )
        if client is None:
            failure_message = "Topic not bound. Use /bind <repo_id> or /bind <path>."
            if send_failure_response:
                await handlers._send_message(
                    message.chat_id,
                    failure_message,
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
            return _TurnRunFailure(
                failure_message,
                prepared_placeholder_id,
                transcript_message_id,
                transcript_text,
            )
        try:
            thread_result = await client.thread_start(
                record.workspace_path, agent=agent
            )
        except Exception as exc:
            log_event(
                handlers._logger,
                logging.WARNING,
                "telegram.turn.thread_start.failed",
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                exc=exc,
            )
            failure_message = "Failed to start a new thread; check logs for details."
            if send_failure_response:
                await handlers._send_message(
                    message.chat_id,
                    failure_message,
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
            return _TurnRunFailure(
                failure_message,
                prepared_placeholder_id,
                transcript_message_id,
                transcript_text,
            )
        if not await handlers._require_thread_workspace(
            message,
            record.workspace_path,
            thread_result,
            action="thread_start",
        ):
            if prepared_placeholder_id is not None:
                await handlers._delete_message(message.chat_id, prepared_placeholder_id)
            return _TurnRunFailure(
                "Failed to start a new thread.",
                prepared_placeholder_id,
                transcript_message_id,
                transcript_text,
            )
        current_backend_thread_id = _extract_thread_id(thread_result)
        if not current_backend_thread_id:
            failure_message = "Failed to start a new thread."
            if send_failure_response:
                await handlers._send_message(
                    message.chat_id,
                    failure_message,
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
            return _TurnRunFailure(
                failure_message,
                prepared_placeholder_id,
                transcript_message_id,
                transcript_text,
            )
        apply_thread_result = getattr(handlers, "_apply_thread_result", None)
        if callable(apply_thread_result):
            updated_record = await apply_thread_result(
                message.chat_id,
                message.thread_id,
                thread_result,
                active_thread_id=current_backend_thread_id,
            )
            if updated_record is not None:
                record = updated_record
    orchestration_service, thread = await _resolve_telegram_managed_thread(
        handlers,
        surface_key=topic_key,
        workspace_root=workspace_root,
        agent=agent,
        repo_id=repo_id or None,
        resource_kind=getattr(record, "resource_kind", None),
        resource_id=getattr(record, "resource_id", None),
        mode=mode,
        pma_enabled=pma_enabled,
        backend_thread_id=current_backend_thread_id,
        allow_new_thread=allow_new_thread,
    )
    if thread is None:
        failure_message = (
            missing_thread_message or "No active thread. Use /new to start one."
        )
        if send_failure_response:
            await handlers._deliver_turn_response(
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                reply_to=message.message_id,
                placeholder_id=prepared_placeholder_id,
                response=failure_message,
            )
        return _TurnRunFailure(
            failure_message,
            prepared_placeholder_id,
            transcript_message_id,
            transcript_text,
        )
    if execution_prompt is None:
        execution_prompt = (
            f"{format_pma_discoverability_preamble(hub_root=handlers._config.root)}"
            "<user_message>\n"
            f"{prompt_text}\n"
            "</user_message>\n"
        )
    execution_input_items = _build_managed_thread_input_items(
        execution_prompt,
        input_items,
    )
    pending_seed = None
    if not pma_enabled:
        active_target_id = (
            str(thread.backend_thread_id or "").strip() or current_backend_thread_id
        )
        pending_seed = match_pending_compact_seed(
            record.pending_compact_seed,
            pending_target_id=record.pending_compact_seed_thread_id,
            active_target_id=active_target_id,
        )
        if pending_seed:
            if execution_input_items is None:
                execution_input_items = [
                    {"type": "text", "text": pending_seed},
                    {"type": "text", "text": execution_prompt},
                ]
            else:
                execution_input_items = [
                    {"type": "text", "text": pending_seed},
                    *execution_input_items,
                ]
    try:
        started_execution = await begin_runtime_thread_execution(
            orchestration_service,
            MessageRequest(
                target_id=thread.thread_target_id,
                target_kind="thread",
                message_text=prompt_text,
                busy_policy="queue",
                model=record.model,
                reasoning=record.effort,
                approval_mode=approval_policy,
                input_items=execution_input_items,
                metadata={
                    "runtime_prompt": execution_prompt,
                    "execution_error_message": public_execution_error,
                },
            ),
            client_request_id=(f"telegram:{topic_key}:{secrets.token_hex(6)}"),
            sandbox_policy=sandbox_policy,
        )
    except Exception as exc:
        failure_message = _sanitize_runtime_thread_result_error(
            exc,
            public_error=public_execution_error,
            timeout_error=timeout_error,
            interrupted_error=interrupted_error,
        )
        if send_failure_response:
            await handlers._deliver_turn_response(
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                reply_to=message.message_id,
                placeholder_id=prepared_placeholder_id,
                response=failure_message,
            )
        return _TurnRunFailure(
            failure_message,
            prepared_placeholder_id,
            transcript_message_id,
            transcript_text,
        )
    if pma_enabled:
        _sync_pma_registry_thread_id(
            handlers,
            record,
            message,
            str(getattr(started_execution.thread, "backend_thread_id", "") or "")
            or None,
        )
    if pending_seed and not pma_enabled:
        await handlers._router.update_topic(
            message.chat_id,
            message.thread_id,
            _clear_pending_compact_seed,
        )

    if (
        str(getattr(started_execution.execution, "status", "") or "").strip()
        == "queued"
    ):
        _ensure_telegram_managed_thread_queue_worker(
            handlers,
            orchestration_service=orchestration_service,
            managed_thread_id=thread.thread_target_id,
            surface_key=topic_key,
            record=record,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            public_execution_error=public_execution_error,
            timeout_error=timeout_error,
            interrupted_error=interrupted_error,
        )
        return _TurnRunResult(
            record=record,
            thread_id=str(thread.backend_thread_id or "") or None,
            turn_id=None,
            response="Queued (waiting for available worker...)",
            placeholder_id=prepared_placeholder_id,
            elapsed_seconds=0.0,
            token_usage=None,
            transcript_message_id=transcript_message_id,
            transcript_text=transcript_text,
        )

    backend_thread_id = str(started_execution.thread.backend_thread_id or "").strip()
    if not backend_thread_id:
        backend_thread_id = str(started_execution.thread.thread_target_id or "").strip()
    backend_turn_id = str(started_execution.execution.backend_id or "").strip()
    if not backend_turn_id:
        backend_turn_id = str(started_execution.execution.execution_id or "").strip()
    turn_key = (
        handlers._turn_key(backend_thread_id, backend_turn_id)
        if backend_thread_id and backend_turn_id
        else None
    )
    registered_turn_key: Optional[tuple[str, str]] = None
    intermediate_response = ""
    if turn_key is not None:
        from ...types import TurnContext

        ctx = TurnContext(
            topic_key=topic_key,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            codex_thread_id=backend_thread_id,
            reply_to_message_id=message.message_id,
            placeholder_message_id=prepared_placeholder_id,
        )
        if handlers._register_turn_context(turn_key, backend_turn_id, ctx):
            registered_turn_key = turn_key
            runtime.current_turn_id = backend_turn_id
            runtime.current_turn_key = turn_key
            await handlers._start_turn_progress(
                turn_key,
                ctx=ctx,
                agent=handlers._effective_agent(record),
                model=record.model,
                label="working",
            )

    try:
        finalized = await _finalize_telegram_managed_thread_execution(
            handlers,
            orchestration_service=orchestration_service,
            started=started_execution,
            surface_key=topic_key,
            chat_id=message.chat_id,
            thread_id=message.thread_id,
            public_execution_error=public_execution_error,
            timeout_error=timeout_error,
            interrupted_error=interrupted_error,
            runtime_event_state=RuntimeThreadRunEventState(),
            on_progress_event=(
                (
                    lambda run_event: handlers._apply_run_event_to_progress(
                        registered_turn_key,
                        run_event,
                    )
                )
                if registered_turn_key is not None
                else None
            ),
        )
    finally:
        if registered_turn_key is not None:
            render_turn_progress_summary = getattr(
                handlers, "_render_turn_progress_summary", None
            )
            if callable(render_turn_progress_summary):
                intermediate_response = render_turn_progress_summary(
                    registered_turn_key
                )
            else:
                render_final_turn_progress = getattr(
                    handlers, "_render_final_turn_progress", None
                )
                if callable(render_final_turn_progress):
                    intermediate_response = render_final_turn_progress(
                        registered_turn_key
                    )
            handlers._turn_contexts.pop(registered_turn_key, None)
            handlers._clear_thinking_preview(registered_turn_key)
            handlers._clear_turn_progress(registered_turn_key)
        runtime.current_turn_id = None
        runtime.current_turn_key = None
        runtime.interrupt_requested = False

    _ensure_telegram_managed_thread_queue_worker(
        handlers,
        orchestration_service=orchestration_service,
        managed_thread_id=thread.thread_target_id,
        surface_key=topic_key,
        record=record,
        chat_id=message.chat_id,
        thread_id=message.thread_id,
        public_execution_error=public_execution_error,
        timeout_error=timeout_error,
        interrupted_error=interrupted_error,
    )
    if finalized["status"] != "ok":
        failure_message = str(finalized.get("error") or public_execution_error)
        interrupt_status_fallback_text: Optional[str] = None
        if finalized["status"] == "interrupted":
            failure_message = _compose_interrupt_response(failure_message)
            if (
                runtime.interrupt_message_id is not None
                and runtime.interrupt_turn_id == backend_turn_id
            ):
                interrupt_status_fallback_text = "Interrupted."
        elif runtime.interrupt_turn_id == backend_turn_id:
            interrupt_status_fallback_text = "Interrupt requested; turn completed."
        response_sent = False
        if send_failure_response:
            response_sent = await handlers._deliver_turn_response(
                chat_id=message.chat_id,
                thread_id=message.thread_id,
                reply_to=message.message_id,
                placeholder_id=prepared_placeholder_id,
                response=failure_message,
            )
        if interrupt_status_fallback_text:
            await handlers._clear_interrupt_status_message(
                chat_id=message.chat_id,
                runtime=runtime,
                turn_id=backend_turn_id,
                fallback_text=interrupt_status_fallback_text,
                outcome_visible=response_sent,
            )
        return _TurnRunFailure(
            failure_message,
            prepared_placeholder_id,
            transcript_message_id,
            transcript_text,
        )
    resolved_backend_thread_id = str(finalized.get("backend_thread_id") or "") or None
    if pma_enabled and resolved_backend_thread_id:
        _sync_pma_registry_thread_id(
            handlers,
            record,
            message,
            resolved_backend_thread_id,
        )
    if not pma_enabled and resolved_backend_thread_id and hasattr(handlers, "_router"):
        await _sync_telegram_thread_binding(
            handlers,
            surface_key=topic_key,
            workspace_root=workspace_root,
            agent=agent,
            repo_id=repo_id or None,
            resource_kind=getattr(record, "resource_kind", None),
            resource_id=getattr(record, "resource_id", None),
            backend_thread_id=resolved_backend_thread_id,
            mode=mode,
            pma_enabled=False,
        )
        user_preview = _preview_from_text(prompt_text, RESUME_PREVIEW_USER_LIMIT)
        assistant_preview = _preview_from_text(
            str(finalized.get("assistant_text") or ""),
            RESUME_PREVIEW_ASSISTANT_LIMIT,
        )

        def _apply_state(updated: "TelegramTopicRecord") -> None:
            updated.active_thread_id = resolved_backend_thread_id
            if resolved_backend_thread_id in updated.thread_ids:
                updated.thread_ids.remove(resolved_backend_thread_id)
            updated.thread_ids.insert(0, resolved_backend_thread_id)
            if len(updated.thread_ids) > MAX_TOPIC_THREAD_HISTORY:
                updated.thread_ids = updated.thread_ids[:MAX_TOPIC_THREAD_HISTORY]
            _set_thread_summary(
                updated,
                resolved_backend_thread_id,
                user_preview=user_preview,
                assistant_preview=assistant_preview,
                last_used_at=now_iso(),
                workspace_path=updated.workspace_path,
                rollout_path=updated.rollout_path,
            )

        record = await handlers._router.update_topic(
            message.chat_id,
            message.thread_id,
            _apply_state,
        )
    response_text = str(finalized.get("assistant_text") or "")
    interrupt_status_fallback_text = None
    if runtime.interrupt_turn_id == backend_turn_id:
        interrupt_status_fallback_text = "Interrupt requested; turn completed."
    return _TurnRunResult(
        record=record,
        thread_id=resolved_backend_thread_id,
        turn_id=backend_turn_id or None,
        response=response_text,
        placeholder_id=prepared_placeholder_id,
        elapsed_seconds=None,
        token_usage=cast(Optional[dict[str, Any]], finalized.get("token_usage")),
        transcript_message_id=transcript_message_id,
        transcript_text=transcript_text,
        intermediate_response=intermediate_response,
        interrupt_status_turn_id=backend_turn_id or None,
        interrupt_status_fallback_text=interrupt_status_fallback_text,
    )


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
        self,
        prompt_text: str,
        record: Any,
        *,
        link_source_text: Optional[str] = None,
        allow_cross_repo: bool = False,
    ) -> tuple[str, bool]:
        if not prompt_text or not record or not record.workspace_path:
            return prompt_text, False
        return await maybe_inject_github_context(
            prompt_text=prompt_text,
            link_source_text=link_source_text or prompt_text,
            workspace_root=Path(record.workspace_path),
            logger=self._logger,
            event_prefix="telegram.github_context",
            allow_cross_repo=allow_cross_repo,
        )

    def _maybe_inject_prompt_context(self, prompt_text: str) -> tuple[str, bool]:
        return maybe_inject_prompt_writing_hint(prompt_text)

    def _maybe_inject_car_context(self, prompt_text: str) -> tuple[str, bool]:
        return maybe_inject_car_awareness(
            prompt_text,
            declared_profile="car_ambient",
        )

    def _maybe_inject_outbox_context(
        self,
        prompt_text: str,
        *,
        record: "TelegramTopicRecord",
        topic_key: str,
    ) -> tuple[str, bool]:
        inbox_dir = self._files_inbox_dir(record.workspace_path, topic_key)
        outbox_dir = self._files_outbox_pending_dir(record.workspace_path, topic_key)
        topic_dir = self._files_topic_dir(record.workspace_path, topic_key)
        return maybe_inject_filebox_hint(
            prompt_text,
            hint_text=wrap_injected_context(
                FILES_HINT_TEMPLATE.format(
                    inbox=str(inbox_dir),
                    outbox=str(outbox_dir),
                    topic_key=topic_key,
                    topic_dir=str(topic_dir),
                    max_bytes=self._config.media.max_file_bytes,
                )
            ),
            has_file_context=True,
        )

    def _has_turn_file_context(
        self,
        message: TelegramMessage,
        prompt_text: str,
        input_items: Optional[list[dict[str, Any]]],
    ) -> bool:
        if has_file_context_signal(prompt_text):
            return True
        if message.photos or message.document or message.audio or message.voice:
            return True
        if not input_items:
            return False
        text_item_types = {"text", "input_text"}
        for item in input_items:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if isinstance(item_type, str) and item_type.lower() in text_item_types:
                continue
            return True
        return False

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
        pma_thread_registry: Optional[AppServerThreadRegistry] = None,
        pma_thread_key: Optional[str] = None,
    ) -> _TurnRunResult | _TurnRunFailure:
        supervisor = getattr(self, "_opencode_supervisor", None)
        turn_delivery_state: dict[str, str] = {}
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

        pma_mode = bool(pma_thread_registry and pma_thread_key)
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

                if pma_mode:
                    pma_thread_registry.set_thread_id(pma_thread_key, thread_id)
                else:
                    record = await self._router.update_topic(
                        message.chat_id, message.thread_id, apply
                    )
            else:
                if not pma_mode:
                    record = await self._router.set_active_thread(
                        message.chat_id, message.thread_id, thread_id
                    )

            if not pma_mode:
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

            pending_seed = match_pending_compact_seed(
                record.pending_compact_seed,
                pending_target_id=record.pending_compact_seed_thread_id,
                active_target_id=thread_id,
            )
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
                            await asyncio.wait_for(
                                opencode_client.abort(thread_id), timeout=10
                            )
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
                        elif part_type == "text":
                            if delta_text:
                                tracker.note_output(delta_text)
                            else:
                                raw_text = part.get("text")
                                if isinstance(raw_text, str) and raw_text:
                                    tracker.note_output(raw_text)
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
                                    last_usage = token_usage.get("last")
                                    if isinstance(last_usage, dict):
                                        token_usage["total"] = dict(last_usage)
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

                    ready_event = asyncio.Event()
                    sse_ready_at: Optional[float] = None
                    output_task = asyncio.create_task(
                        collect_opencode_output(
                            opencode_client,
                            session_id=thread_id,
                            workspace_path=str(workspace_root),
                            model_payload=model_payload,
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
                            ready_event=ready_event,
                            stall_timeout_seconds=self._opencode_session_stall_timeout_seconds(),
                        )
                    )
                    sse_ready_at = time.monotonic()
                    with suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(ready_event.wait(), timeout=2.0)
                    sse_ready_ms = int((time.monotonic() - sse_ready_at) * 1000)
                    log_event(
                        self._logger,
                        logging.INFO,
                        "telegram.opencode.sse_ready",
                        topic_key=key,
                        chat_id=message.chat_id,
                        thread_id=message.thread_id,
                        codex_thread_id=thread_id,
                        sse_ready_ms=sse_ready_ms,
                    )
                    timeout_seconds = self._config.agent_turn_timeout_seconds.get(
                        "opencode"
                    )
                    timeout_task: Optional[asyncio.Task] = None
                    if timeout_seconds is not None and timeout_seconds > 0:
                        timeout_task = asyncio.create_task(
                            asyncio.sleep(timeout_seconds)
                        )
                    prompt_sent_at = time.monotonic()
                    prompt_task = asyncio.create_task(
                        opencode_client.prompt_async(
                            thread_id,
                            message=prompt_text,
                            model=model_payload,
                        )
                    )
                    try:
                        await prompt_task
                        prompt_send_ms = int((time.monotonic() - prompt_sent_at) * 1000)
                        log_event(
                            self._logger,
                            logging.INFO,
                            "telegram.opencode.prompt_sent",
                            topic_key=key,
                            chat_id=message.chat_id,
                            thread_id=message.thread_id,
                            codex_thread_id=thread_id,
                            prompt_send_ms=prompt_send_ms,
                            endpoint="/session/{id}/prompt_async",
                        )
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
                            turn_elapsed_seconds = time.monotonic() - turn_started_at
                            completion_mode = (
                                "timeout"
                                if not runtime.interrupt_requested
                                else "interrupt"
                            )
                            log_event(
                                self._logger,
                                logging.INFO,
                                "telegram.opencode.completed",
                                topic_key=key,
                                chat_id=message.chat_id,
                                thread_id=message.thread_id,
                                codex_thread_id=thread_id,
                                completion_mode=completion_mode,
                                elapsed_seconds=turn_elapsed_seconds,
                            )
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
                    log_event(
                        self._logger,
                        logging.INFO,
                        "telegram.opencode.completed",
                        topic_key=key,
                        chat_id=message.chat_id,
                        thread_id=message.thread_id,
                        codex_thread_id=thread_id,
                        completion_mode="normal",
                        elapsed_seconds=turn_elapsed_seconds,
                    )
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
                intermediate_response=turn_delivery_state.get(
                    "intermediate_response", ""
                ),
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
                render_turn_progress_summary = getattr(
                    self, "_render_turn_progress_summary", None
                )
                if callable(render_turn_progress_summary):
                    turn_delivery_state["intermediate_response"] = (
                        render_turn_progress_summary(turn_key)
                    )
                else:
                    render_final_turn_progress = getattr(
                        self, "_render_final_turn_progress", None
                    )
                    if callable(render_final_turn_progress):
                        turn_delivery_state["intermediate_response"] = (
                            render_final_turn_progress(turn_key)
                        )
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
        pma_thread_registry: Optional[AppServerThreadRegistry] = None,
        pma_thread_key: Optional[str] = None,
    ) -> _TurnRunResult | _TurnRunFailure:
        turn_handle = None
        turn_key: Optional[TurnKey] = None
        turn_started_at: Optional[float] = None
        turn_delivery_state: dict[str, str] = {}

        def _is_missing_thread_error(exc: Exception) -> bool:
            if not isinstance(exc, CodexAppServerResponseError):
                return False
            message = str(exc).lower()
            missing_markers = (
                "thread not found",
                "no rollout found for thread id",
            )
            return any(marker in message for marker in missing_markers)

        async def _start_new_thread(agent: str) -> Optional[str]:
            nonlocal record
            workspace_path = record.workspace_path
            if not workspace_path:
                return None
            thread = await client.thread_start(workspace_path, agent=agent)
            if not await self._require_thread_workspace(
                message, workspace_path, thread, action="thread_start"
            ):
                return None
            new_thread_id = _extract_thread_id(thread)
            if not new_thread_id:
                return None
            if pma_mode and pma_thread_registry and pma_thread_key:
                pma_thread_registry.set_thread_id(pma_thread_key, new_thread_id)
            elif not pma_mode:
                record = await self._apply_thread_result(
                    message.chat_id,
                    message.thread_id,
                    thread,
                    active_thread_id=new_thread_id,
                )
            return new_thread_id

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

        pma_mode = bool(pma_thread_registry and pma_thread_key)
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
                agent = self._effective_agent(record)
                thread_id = await _start_new_thread(agent)
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
            else:
                if not pma_mode:
                    record = await self._router.set_active_thread(
                        message.chat_id, message.thread_id, thread_id
                    )

            if thread_id and not pma_mode:
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
            if not pma_mode:
                pending_seed = match_pending_compact_seed(
                    record.pending_compact_seed,
                    pending_target_id=record.pending_compact_seed_thread_id,
                    active_target_id=thread_id,
                )
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

                try:
                    turn_handle = await client.turn_start(
                        thread_id,
                        prompt_text,
                        input_items=input_items,
                        approval_policy=approval_policy,
                        sandbox_policy=sandbox_policy,
                        **turn_kwargs,
                    )
                except Exception as exc:
                    if (
                        pma_mode
                        and _is_missing_thread_error(exc)
                        and pma_thread_registry
                        and pma_thread_key
                    ):
                        log_event(
                            self._logger,
                            logging.WARNING,
                            "telegram.pma.thread.reset",
                            topic_key=key,
                            chat_id=message.chat_id,
                            thread_id=message.thread_id,
                            codex_thread_id=thread_id,
                            reason="thread_not_found",
                        )
                        pma_thread_registry.reset_thread(pma_thread_key)
                        if not allow_new_thread:
                            failure_message = (
                                "PMA thread no longer exists. Send a new message to "
                                "start a PMA thread, then retry /compact."
                            )
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
                        agent = self._effective_agent(record)
                        thread_id = await _start_new_thread(agent)
                        if thread_id is None:
                            raise
                        turn_handle = await client.turn_start(
                            thread_id,
                            prompt_text,
                            input_items=input_items,
                            approval_policy=approval_policy,
                            sandbox_policy=sandbox_policy,
                            **turn_kwargs,
                        )
                    else:
                        raise
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
                    timeout_seconds=self._config.agent_turn_timeout_seconds.get(
                        "codex"
                    ),
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
                    render_turn_progress_summary = getattr(
                        self, "_render_turn_progress_summary", None
                    )
                    if callable(render_turn_progress_summary):
                        turn_delivery_state["intermediate_response"] = (
                            render_turn_progress_summary(turn_key)
                        )
                    else:
                        render_final_turn_progress = getattr(
                            self, "_render_final_turn_progress", None
                        )
                        if callable(render_final_turn_progress):
                            turn_delivery_state["intermediate_response"] = (
                                render_final_turn_progress(turn_key)
                            )
                    self._turn_contexts.pop(turn_key, None)
                    self._clear_thinking_preview(turn_key)
                    self._clear_turn_progress(turn_key)
            runtime.current_turn_id = None
            runtime.current_turn_key = None
            runtime.interrupt_requested = False

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

        turn_handle_id = turn_handle.turn_id if turn_handle else None
        interrupt_status_fallback_text = None
        if is_interrupt_status(result.status):
            response = _compose_interrupt_response(response)
            if (
                runtime.interrupt_message_id is not None
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
            intermediate_response=turn_delivery_state.get("intermediate_response", ""),
            interrupt_status_turn_id=turn_handle_id,
            interrupt_status_fallback_text=interrupt_status_fallback_text,
        )

    def _prepare_turn_prompt(
        self, prompt_text: str, *, transcript_text: Optional[str] = None
    ) -> str:
        prompt_text = self._maybe_append_whisper_disclaimer(
            prompt_text, transcript_text=transcript_text
        )
        return prompt_text

    def _pma_registry_key(
        self, record: "TelegramTopicRecord", message: Optional[TelegramMessage] = None
    ) -> str:
        """
        Return PMA thread registry key.

        Thread scoping decision:
        - When require_topics is false (default): use global keys (pma/pma.opencode).
          All Telegram topics share one PMA conversation per agent.
        - When require_topics is true: use per-topic keys (pma.{topic_key}/pma.opencode.{topic_key}).
          Each Telegram topic gets its own isolated PMA conversation.

        This allows hubs with multiple topics to maintain separate PMA contexts
        when require_topics is enabled, while keeping a single shared context
        in the common case (require_topics disabled).
        """
        agent = self._effective_agent(record)
        base_key = pma_base_key(agent)

        require_topics = getattr(self._config, "require_topics", False)
        if require_topics and message is not None:
            return pma_topic_scoped_key(
                agent, message.chat_id, message.thread_id, topic_key_fn=build_topic_key
            )
        return base_key

    async def _prepare_pma_prompt(
        self,
        message_text: str,
        *,
        record: "TelegramTopicRecord",
        message: TelegramMessage,
    ) -> Optional[str]:
        hub_root = getattr(self, "_hub_root", None)
        if hub_root is None:
            return None
        supervisor = getattr(self, "_hub_supervisor", None)
        snapshot = await build_hub_snapshot(supervisor, hub_root=Path(hub_root))
        base_prompt = load_pma_prompt(hub_root)
        prompt_state_key = self._pma_registry_key(record, message)
        return format_pma_prompt(
            base_prompt,
            snapshot,
            message_text,
            hub_root=hub_root,
            prompt_state_key=prompt_state_key,
        )

    async def _prepare_turn_context(
        self,
        message: TelegramMessage,
        prompt_text: str,
        record: "TelegramTopicRecord",
        *,
        input_items: Optional[list[dict[str, Any]]] = None,
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

        if self._has_turn_file_context(message, prompt_text, input_items):
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
        pma_enabled = bool(record and getattr(record, "pma_enabled", False))
        if pma_enabled:
            hub_root = getattr(self, "_hub_root", None)
            if hub_root is None:
                failure_message = "PMA unavailable; hub root not configured."
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
            if record is None:
                from ...state import TelegramTopicRecord

                record = TelegramTopicRecord(pma_enabled=True)
            record = dataclasses.replace(record, workspace_path=str(hub_root))
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

        if record.active_thread_id and not pma_enabled:
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

        pma_thread_registry = (
            getattr(self, "_hub_thread_registry", None) if pma_enabled else None
        )
        pma_thread_key = (
            self._pma_registry_key(record, message) if pma_enabled else None
        )
        thread_id = None if pma_enabled else record.active_thread_id
        if pma_enabled and pma_thread_registry and pma_thread_key:
            thread_id = pma_thread_registry.get_thread_id(pma_thread_key)
        prompt_text = (
            text_override if text_override is not None else (message.text or "")
        )
        prompt_text = format_forwarded_telegram_message_text(message, prompt_text)
        prompt_text = self._prepare_turn_prompt(
            prompt_text, transcript_text=transcript_text
        )
        if pma_enabled:
            user_message_prompt = prompt_text
            pma_prompt = await self._prepare_pma_prompt(
                prompt_text,
                record=record,
                message=message,
            )
            if pma_prompt is None:
                failure_message = "PMA unavailable; hub snapshot failed."
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
            prompt_text, injected = await self._maybe_inject_github_context(
                pma_prompt,
                record,
                link_source_text=user_message_prompt,
                allow_cross_repo=True,
            )
            if injected:
                await self._send_message(
                    message.chat_id,
                    "gh CLI used, github context injected",
                    thread_id=message.thread_id,
                    reply_to=message.message_id,
                )
        else:
            prompt_text, key = await self._prepare_turn_context(
                message, prompt_text, record, input_items=input_items
            )

        if (
            pma_enabled
            and getattr(self._config, "root", None) is not None
            and callable(getattr(self, "_spawn_task", None))
        ):
            approval_policy, sandbox_policy = self._effective_policies(record)
            return await _run_telegram_managed_thread_turn(
                self,
                message=message,
                runtime=runtime,
                record=record,
                topic_key=key,
                prompt_text=prompt_text,
                input_items=input_items,
                send_placeholder=send_placeholder,
                send_failure_response=send_failure_response,
                transcript_message_id=transcript_message_id,
                transcript_text=transcript_text,
                placeholder_id=placeholder_id,
                allow_new_thread=allow_new_thread,
                missing_thread_message=missing_thread_message,
                approval_policy=approval_policy,
                sandbox_policy=sandbox_policy,
            )

        if getattr(self._config, "root", None) is not None and callable(
            getattr(self, "_spawn_task", None)
        ):
            approval_policy, sandbox_policy = self._effective_policies(record)
            return await _run_telegram_managed_thread_turn(
                self,
                message=message,
                runtime=runtime,
                record=record,
                topic_key=key,
                prompt_text=prompt_text,
                input_items=input_items,
                send_placeholder=send_placeholder,
                send_failure_response=send_failure_response,
                transcript_message_id=transcript_message_id,
                transcript_text=transcript_text,
                placeholder_id=placeholder_id,
                allow_new_thread=allow_new_thread,
                missing_thread_message=missing_thread_message,
                mode="repo",
                pma_enabled=False,
                execution_prompt=prompt_text,
                public_execution_error=TELEGRAM_REPO_PUBLIC_EXECUTION_ERROR,
                timeout_error=TELEGRAM_REPO_TIMEOUT_ERROR,
                interrupted_error=TELEGRAM_REPO_INTERRUPTED_ERROR,
                approval_policy=approval_policy,
                sandbox_policy=sandbox_policy,
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
                pma_thread_registry=pma_thread_registry,
                pma_thread_key=pma_thread_key,
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
            pma_thread_registry=pma_thread_registry,
            pma_thread_key=pma_thread_key,
        )
