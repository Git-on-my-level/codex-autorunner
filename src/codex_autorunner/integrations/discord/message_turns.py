from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Awaitable, Optional, cast

from ...agents.registry import get_registered_agents, wrap_requested_agent_context
from ...core.context_awareness import (
    maybe_inject_car_awareness,
    maybe_inject_filebox_hint,
    maybe_inject_prompt_writing_hint,
)
from ...core.filebox import inbox_dir, outbox_dir, outbox_pending_dir
from ...core.injected_context import wrap_injected_context
from ...core.logging_utils import log_event
from ...core.orchestration import (
    FlowTarget,
    MessageRequest,
    PausedFlowTarget,
    SurfaceThreadMessageRequest,
    build_harness_backed_orchestration_service,
    build_surface_orchestration_ingress,
)
from ...core.orchestration.runtime_thread_events import RuntimeThreadRunEventState
from ...core.orchestration.runtime_threads import (
    RuntimeThreadExecution,
    begin_next_queued_runtime_thread_execution,
    begin_runtime_thread_execution,
)
from ...core.pma_context import (
    build_hub_snapshot,
    format_pma_discoverability_preamble,
    format_pma_prompt,
    load_pma_prompt,
)
from ...core.pma_notification_store import (
    PmaNotificationStore,
    build_notification_context_block,
    notification_surface_key,
)
from ...core.pma_thread_store import PmaThreadStore
from ...core.ports.run_event import TokenUsage
from ...core.utils import canonicalize_path
from ...integrations.chat.agents import resolve_chat_runtime_agent
from ...integrations.chat.approval_modes import resolve_approval_mode_policies
from ...integrations.chat.collaboration_policy import CollaborationEvaluationResult
from ...integrations.chat.compaction import match_pending_compact_seed
from ...integrations.chat.dispatcher import DispatchContext
from ...integrations.chat.forwarding import (
    compose_forwarded_message_text,
    compose_inbound_message_text,
)
from ...integrations.chat.models import ChatMessageEvent
from ...integrations.chat.runtime_thread_errors import (
    sanitize_runtime_thread_error,
)
from ..chat.managed_thread_progress import (
    ProgressRuntimeState,
    apply_run_event_to_progress_tracker,
)
from ..chat.managed_thread_turns import (
    ManagedThreadErrorMessages,
    ManagedThreadExecutionHooks,
    ManagedThreadFinalizationResult,
    ManagedThreadQueuedExecutionStarter,
    ManagedThreadQueueWorkerHooks,
    ManagedThreadSurfaceInfo,
    ManagedThreadTargetRequest,
    ManagedThreadTurnCoordinator,
    coerce_managed_thread_finalization_result,
    complete_managed_thread_execution,
)
from ..chat.managed_thread_turns import (
    build_managed_thread_input_items as _shared_build_managed_thread_input_items,
)
from ..chat.managed_thread_turns import (
    resolve_managed_thread_target as _shared_resolve_managed_thread_target,
)
from ..chat.progress_primitives import TurnProgressTracker, render_progress_text
from ..chat.turn_metrics import (
    _extract_context_usage_percent,
    compose_turn_response_with_footer,
)
from .components import build_cancel_turn_button, build_cancel_turn_custom_id
from .rendering import (
    chunk_discord_message,
    format_discord_message,
    sanitize_discord_outbound_text,
    truncate_for_discord,
)

_logger = logging.getLogger(__name__)

DISCORD_PMA_PUBLIC_EXECUTION_ERROR = "Discord PMA turn failed"
DISCORD_REPO_PUBLIC_EXECUTION_ERROR = "Discord turn failed"
DISCORD_PMA_TIMEOUT_SECONDS = 7200
DISCORD_MANAGED_THREAD_SUBMISSION_TIMEOUT_SECONDS = 45.0
DISCORD_PMA_PROGRESS_MAX_ACTIONS = 12
DISCORD_PMA_PROGRESS_MIN_EDIT_INTERVAL_SECONDS = 1.0
DISCORD_PMA_PROGRESS_HEARTBEAT_INTERVAL_SECONDS = 2.0


class DiscordTurnStartupFailure(RuntimeError):
    """Raised after a Discord turn startup failure has been surfaced to the user."""


@dataclass(frozen=True)
class DiscordMessageTurnResult:
    final_message: str
    preview_message_id: Optional[str] = None
    intermediate_message: Optional[str] = None
    token_usage: Optional[dict[str, Any]] = None
    elapsed_seconds: Optional[float] = None
    send_final_message: bool = True
    deferred_delivery: bool = False


@dataclass(frozen=True)
class _DiscordProgressReuseRequest:
    source_message_id: str
    acknowledgement: str


@dataclass(frozen=True)
class _DiscordReusableProgressMessage:
    source_message_id: str
    channel_id: str
    message_id: str


@dataclass
class _DiscordOrchestrationState:
    progress_reuse_requests: dict[str, _DiscordProgressReuseRequest]
    reusable_progress_messages: dict[str, _DiscordReusableProgressMessage]
    thread_queue_tasks: dict[str, asyncio.Task[Any]]


@dataclass(frozen=True)
class _DiscordMessageTurnDispatch:
    service: Any
    event: ChatMessageEvent
    context: DispatchContext
    binding: dict[str, object]
    channel_id: str
    text: str
    has_attachments: bool
    log_event_fn: Any
    build_ticket_flow_controller_fn: Any
    ensure_worker_fn: Any
    workspace_root: Path
    pma_enabled: bool
    effective_pma_enabled: bool
    notification_reply: Any
    agent: str
    agent_profile: Optional[str]
    runtime_agent: str
    model_override: Optional[str]
    reasoning_effort: Optional[str]
    session_key: str
    pending_compact_seed: Optional[str]
    turn_text: str
    flow_reply_text: str
    paused_records: dict[str, Any] = field(default_factory=dict)

    def build_request(
        self,
        *,
        workspace_root: Optional[Path] = None,
        pma_enabled: Optional[bool] = None,
    ) -> SurfaceThreadMessageRequest:
        return SurfaceThreadMessageRequest(
            surface_kind="discord",
            workspace_root=workspace_root or self.workspace_root,
            prompt_text=self.turn_text,
            agent_id=self.runtime_agent,
            pma_enabled=(
                self.effective_pma_enabled if pma_enabled is None else pma_enabled
            ),
        )


_sanitize_runtime_thread_result_error = sanitize_runtime_thread_error


def _get_discord_progress_reuse_requests(
    service: Any,
) -> dict[str, _DiscordProgressReuseRequest]:
    return _discord_orchestration_state(service).progress_reuse_requests


def _get_discord_reusable_progress_messages(
    service: Any,
) -> dict[str, _DiscordReusableProgressMessage]:
    return _discord_orchestration_state(service).reusable_progress_messages


def _discord_orchestration_state(service: Any) -> _DiscordOrchestrationState:
    requests = getattr(service, "_discord_turn_progress_reuse_requests", None)
    if not isinstance(requests, dict):
        requests = {}
        service._discord_turn_progress_reuse_requests = requests
    messages = getattr(service, "_discord_reusable_progress_messages", None)
    if not isinstance(messages, dict):
        messages = {}
        service._discord_reusable_progress_messages = messages
    task_map = getattr(service, "_discord_thread_queue_tasks", None)
    if not isinstance(task_map, dict):
        task_map = {}
        service._discord_thread_queue_tasks = task_map
        service._discord_managed_thread_queue_tasks = task_map
    return _DiscordOrchestrationState(
        progress_reuse_requests=requests,
        reusable_progress_messages=messages,
        thread_queue_tasks=task_map,
    )


def request_discord_turn_progress_reuse(
    service: Any,
    *,
    thread_target_id: str,
    source_message_id: str,
    acknowledgement: str,
) -> None:
    normalized_thread_target_id = str(thread_target_id or "").strip()
    normalized_source_message_id = str(source_message_id or "").strip()
    normalized_acknowledgement = str(acknowledgement or "").strip()
    if (
        not normalized_thread_target_id
        or not normalized_source_message_id
        or not normalized_acknowledgement
    ):
        return
    _get_discord_progress_reuse_requests(service)[normalized_thread_target_id] = (
        _DiscordProgressReuseRequest(
            source_message_id=normalized_source_message_id,
            acknowledgement=normalized_acknowledgement,
        )
    )


def clear_discord_turn_progress_reuse(
    service: Any,
    *,
    thread_target_id: str,
) -> None:
    normalized_thread_target_id = str(thread_target_id or "").strip()
    if not normalized_thread_target_id:
        return
    _get_discord_progress_reuse_requests(service).pop(normalized_thread_target_id, None)
    _get_discord_reusable_progress_messages(service).pop(
        normalized_thread_target_id, None
    )


def _peek_discord_progress_reuse_request(
    service: Any,
    *,
    thread_target_id: str,
) -> Optional[_DiscordProgressReuseRequest]:
    normalized_thread_target_id = str(thread_target_id or "").strip()
    if not normalized_thread_target_id:
        return None
    request = _get_discord_progress_reuse_requests(service).get(
        normalized_thread_target_id
    )
    if isinstance(request, _DiscordProgressReuseRequest):
        return request
    return None


def _stash_discord_reusable_progress_message(
    service: Any,
    *,
    thread_target_id: str,
    source_message_id: str,
    channel_id: str,
    message_id: str,
) -> None:
    normalized_thread_target_id = str(thread_target_id or "").strip()
    normalized_source_message_id = str(source_message_id or "").strip()
    normalized_channel_id = str(channel_id or "").strip()
    normalized_message_id = str(message_id or "").strip()
    if (
        not normalized_thread_target_id
        or not normalized_source_message_id
        or not normalized_channel_id
        or not normalized_message_id
    ):
        return
    _get_discord_reusable_progress_messages(service)[normalized_thread_target_id] = (
        _DiscordReusableProgressMessage(
            source_message_id=normalized_source_message_id,
            channel_id=normalized_channel_id,
            message_id=normalized_message_id,
        )
    )


def _maybe_inject_discord_filebox_hint(
    prompt_text: str,
    *,
    user_text: str,
    workspace_root: Path,
) -> tuple[str, bool]:
    """Inject repo FileBox paths when the raw Discord turn requests them."""
    hint_text = wrap_injected_context(
        "\n".join(
            [
                f"Inbox: {inbox_dir(workspace_root)}",
                f"Outbox: {outbox_dir(workspace_root)}",
                f"Outbox (pending): {outbox_pending_dir(workspace_root)}",
                "Use inbox files as local inputs and place reply files in outbox.",
            ]
        )
    )
    return maybe_inject_filebox_hint(
        prompt_text,
        hint_text=hint_text,
        user_input_texts=[user_text],
    )


def _claim_discord_reusable_progress_message(
    service: Any,
    *,
    thread_target_id: str,
    source_message_id: Optional[str],
) -> Optional[str]:
    normalized_thread_target_id = str(thread_target_id or "").strip()
    normalized_source_message_id = str(source_message_id or "").strip()
    if not normalized_thread_target_id or not normalized_source_message_id:
        return None
    requests = _get_discord_progress_reuse_requests(service)
    request = requests.get(normalized_thread_target_id)
    if isinstance(request, _DiscordProgressReuseRequest):
        if request.source_message_id != normalized_source_message_id:
            return None
        requests.pop(normalized_thread_target_id, None)
    reusable = _get_discord_reusable_progress_messages(service).pop(
        normalized_thread_target_id, None
    )
    if (
        isinstance(reusable, _DiscordReusableProgressMessage)
        and reusable.source_message_id == normalized_source_message_id
    ):
        return reusable.message_id
    return None


def _managed_thread_surface_key_for_notification_reply(
    notification_reply: Any,
) -> Optional[str]:
    notification_id = getattr(notification_reply, "notification_id", None)
    if isinstance(notification_id, str) and notification_id.strip():
        return notification_surface_key(notification_id)
    return None


def _spawn_discord_background_task(
    service: Any,
    coro: Awaitable[None],
    *,
    await_on_shutdown: bool = False,
) -> asyncio.Task[Any]:
    spawn_task = service._spawn_task
    if not await_on_shutdown:
        return cast(asyncio.Task[Any], spawn_task(coro))
    try:
        return cast(
            asyncio.Task[Any],
            spawn_task(coro, await_on_shutdown=True),
        )
    except TypeError as exc:
        if "await_on_shutdown" not in str(exc):
            raise
        return cast(asyncio.Task[Any], spawn_task(coro))


async def _acknowledge_discord_progress_reuse(
    service: Any,
    *,
    channel_id: str,
    message_id: str,
    acknowledgement: str,
) -> bool:
    try:
        await service._rest.edit_channel_message(
            channel_id=channel_id,
            message_id=message_id,
            payload={
                "content": truncate_for_discord(
                    format_discord_message(acknowledgement),
                    max_len=max(int(service._config.max_message_length), 32),
                ),
                "components": [],
            },
        )
    except (RuntimeError, ConnectionError, OSError):
        return False
    return True


def _resolve_discord_turn_policies(
    binding: Optional[dict[str, Any]],
    *,
    default_approval_policy: str,
    default_sandbox_policy: str,
) -> tuple[str, Any]:
    approval_mode = "yolo"
    explicit_approval_policy: Optional[str] = None
    explicit_sandbox_policy: Optional[Any] = None
    if isinstance(binding, dict):
        binding_mode = str(binding.get("approval_mode") or "").strip()
        if binding_mode:
            approval_mode = binding_mode
        binding_policy = binding.get("approval_policy")
        if isinstance(binding_policy, str) and binding_policy.strip():
            explicit_approval_policy = binding_policy.strip()
        binding_sandbox = binding.get("sandbox_policy")
        if isinstance(binding_sandbox, str) and binding_sandbox.strip():
            explicit_sandbox_policy = binding_sandbox.strip()
    approval_policy, sandbox_policy = resolve_approval_mode_policies(
        approval_mode,
        default_approval_policy=default_approval_policy,
        default_sandbox_policy=default_sandbox_policy,
        override_approval_policy=explicit_approval_policy,
        override_sandbox_policy=explicit_sandbox_policy,
    )
    return approval_policy or default_approval_policy, sandbox_policy


async def _apply_discord_progress_run_event(
    tracker: TurnProgressTracker,
    run_event: Any,
    *,
    runtime_state: ProgressRuntimeState,
    edit_progress: Any,
) -> None:
    if isinstance(run_event, TokenUsage):
        usage_payload = run_event.usage
        if isinstance(usage_payload, dict):
            tracker.context_usage_percent = _extract_context_usage_percent(
                usage_payload
            )
        return
    outcome = apply_run_event_to_progress_tracker(
        tracker,
        run_event,
        runtime_state=runtime_state,
    )
    if not outcome.changed:
        return
    await edit_progress(
        force=outcome.force,
        remove_components=outcome.remove_components,
        render_mode=outcome.render_mode,
    )


async def resolve_bound_workspace_root(
    service: Any,
    *,
    channel_id: str,
) -> tuple[Optional[dict[str, Any]], Optional[Path]]:
    binding = await service._store.get_binding(channel_id=channel_id)
    if binding is None:
        return None, None

    pma_enabled = bool(binding.get("pma_enabled", False))
    workspace_raw = binding.get("workspace_path")
    workspace_root: Optional[Path] = None
    if pma_enabled:
        fallback = canonicalize_path(Path(service._config.root))
        if fallback.exists() and fallback.is_dir():
            workspace_root = fallback

    if (
        workspace_root is None
        and isinstance(workspace_raw, str)
        and workspace_raw.strip()
    ):
        candidate = canonicalize_path(Path(workspace_raw))
        if candidate.exists() and candidate.is_dir():
            workspace_root = candidate

    return binding, workspace_root


def _build_discord_surface_ingress(
    dispatch: _DiscordMessageTurnDispatch,
) -> Any:
    return build_surface_orchestration_ingress(
        event_sink=lambda orchestration_event: dispatch.log_event_fn(
            dispatch.service._logger,
            logging.INFO,
            f"discord.{orchestration_event.event_type}",
            channel_id=dispatch.channel_id,
            conversation_id=dispatch.context.conversation_id,
            surface_kind=orchestration_event.surface_kind,
            target_kind=orchestration_event.target_kind,
            target_id=orchestration_event.target_id,
            status=orchestration_event.status,
            **orchestration_event.metadata,
        )
    )


async def _resolve_discord_paused_flow(
    _request: SurfaceThreadMessageRequest,
    *,
    dispatch: _DiscordMessageTurnDispatch,
) -> Optional[PausedFlowTarget]:
    paused = await dispatch.service._find_paused_flow_run(dispatch.workspace_root)
    if paused is None:
        return None
    if dispatch.service._is_user_ticket_pause(dispatch.workspace_root, paused):
        dispatch.log_event_fn(
            dispatch.service._logger,
            logging.INFO,
            "discord.flow.reply.skipped_for_user_ticket_pause",
            channel_id=dispatch.channel_id,
            run_id=paused.id,
        )
        return None
    dispatch.paused_records[paused.id] = paused
    paused_status = getattr(paused, "status", None)
    return PausedFlowTarget(
        flow_target=FlowTarget(
            flow_target_id="ticket_flow",
            flow_type="ticket_flow",
            display_name="ticket_flow",
            workspace_root=str(dispatch.workspace_root),
        ),
        run_id=paused.id,
        status=(
            str(getattr(paused_status, "value", paused_status))
            if paused_status is not None
            else None
        ),
        workspace_root=dispatch.workspace_root,
    )


async def _submit_discord_flow_reply(
    _request: SurfaceThreadMessageRequest,
    flow_target: PausedFlowTarget,
    *,
    dispatch: _DiscordMessageTurnDispatch,
) -> None:
    paused_record = dispatch.paused_records.get(flow_target.run_id)
    if paused_record is None:
        return
    reply_text = dispatch.flow_reply_text
    if dispatch.has_attachments:
        (
            reply_text,
            saved_attachments,
            failed_attachments,
            transcript_message,
            _native_input_items,
        ) = await dispatch.service._with_attachment_context(
            prompt_text=dispatch.flow_reply_text,
            workspace_root=dispatch.workspace_root,
            attachments=dispatch.event.attachments,
            channel_id=dispatch.channel_id,
        )
        if transcript_message:
            await dispatch.service._send_channel_message_safe(
                dispatch.channel_id,
                {
                    "content": transcript_message,
                    "allowed_mentions": {"parse": []},
                },
            )
        if failed_attachments > 0:
            await dispatch.service._send_channel_message_safe(
                dispatch.channel_id,
                {
                    "content": (
                        "Some Discord attachments could not be downloaded. "
                        "Continuing with available inputs."
                    )
                },
            )
        if not reply_text.strip() and saved_attachments == 0:
            await dispatch.service._send_channel_message_safe(
                dispatch.channel_id,
                {
                    "content": (
                        "Failed to download attachments from Discord. Please retry."
                    ),
                },
            )
            return

    reply_path = dispatch.service._write_user_reply(
        dispatch.workspace_root, paused_record, reply_text
    )
    run_mirror = dispatch.service._flow_run_mirror(dispatch.workspace_root)
    run_mirror.mirror_inbound(
        run_id=flow_target.run_id,
        platform="discord",
        event_type="flow_reply_message",
        kind="command",
        actor="user",
        text=reply_text,
        chat_id=dispatch.channel_id,
        thread_id=dispatch.event.thread.thread_id,
        message_id=dispatch.event.message.message_id,
    )
    controller = dispatch.build_ticket_flow_controller_fn(dispatch.workspace_root)
    try:
        updated = await controller.resume_flow(flow_target.run_id)
    except ValueError as exc:
        await dispatch.service._send_channel_message_safe(
            dispatch.channel_id,
            {"content": f"Failed to resume paused run: {exc}"},
        )
        return
    ensure_result = dispatch.ensure_worker_fn(
        dispatch.workspace_root,
        updated.id,
        is_terminal=updated.status.is_terminal(),
    )
    dispatch.service._close_worker_handles(ensure_result)
    content = format_discord_message(
        f"Reply saved to `{reply_path.name}` and resumed paused run `{updated.id}`."
    )
    await dispatch.service._send_channel_message_safe(
        dispatch.channel_id, {"content": content}
    )
    run_mirror.mirror_outbound(
        run_id=updated.id,
        platform="discord",
        event_type="flow_reply_notice",
        kind="notice",
        actor="car",
        text=content,
        chat_id=dispatch.channel_id,
        thread_id=dispatch.event.thread.thread_id,
    )


async def _submit_discord_thread_message(
    request: SurfaceThreadMessageRequest,
    *,
    dispatch: _DiscordMessageTurnDispatch,
) -> DiscordMessageTurnResult:
    managed_thread_surface_key = _managed_thread_surface_key_for_notification_reply(
        dispatch.notification_reply
    )
    managed_thread_status = _resolve_discord_managed_thread_status(
        dispatch,
        workspace_root=request.workspace_root,
        managed_thread_surface_key=managed_thread_surface_key,
        pma_enabled=request.pma_enabled,
    )

    async def _send_initial_progress_placeholder() -> Optional[str]:
        initial_content = "Received. Preparing turn..."
        if dispatch.has_attachments:
            initial_content = "Preparing attachments..."
            if managed_thread_status.busy:
                initial_content = (
                    "Busy. Preparing attachments while the current turn finishes..."
                )
        try:
            response = await dispatch.service._send_channel_message(
                dispatch.channel_id,
                {"content": initial_content},
            )
        except (RuntimeError, ConnectionError, OSError):
            dispatch.service._logger.warning(
                "Discord initial progress placeholder send failed for channel=%s",
                dispatch.channel_id,
                exc_info=True,
            )
            return None
        message_id = response.get("id")
        return message_id if isinstance(message_id, str) and message_id else None

    async def _resolve_managed_thread_id() -> Optional[str]:
        binding = await dispatch.service._store.get_binding(
            channel_id=dispatch.channel_id
        )
        logical_agent, agent_profile = dispatch.service._resolve_agent_state(binding)
        if not isinstance(logical_agent, str) or not logical_agent.strip():
            logical_agent = dispatch.agent
        repo_id = binding.get("repo_id") if isinstance(binding, dict) else None
        resource_kind = (
            binding.get("resource_kind") if isinstance(binding, dict) else None
        )
        resource_id = binding.get("resource_id") if isinstance(binding, dict) else None
        _orchestration_service, thread = resolve_discord_thread_target(
            dispatch.service,
            channel_id=dispatch.channel_id,
            managed_thread_surface_key=managed_thread_surface_key,
            workspace_root=request.workspace_root,
            agent=logical_agent,
            agent_profile=agent_profile,
            repo_id=repo_id if isinstance(repo_id, str) and repo_id.strip() else None,
            resource_kind=(
                resource_kind.strip()
                if isinstance(resource_kind, str) and resource_kind.strip()
                else None
            ),
            resource_id=(
                resource_id.strip()
                if isinstance(resource_id, str) and resource_id.strip()
                else None
            ),
            mode="pma" if request.pma_enabled else "repo",
            pma_enabled=request.pma_enabled,
        )
        thread_target_id = str(getattr(thread, "thread_target_id", "") or "").strip()
        return thread_target_id or None

    async def _run_in_background() -> None:
        turn_result = await _execute_discord_thread_message(
            request,
            dispatch=dispatch,
            initial_progress_message_id=progress_message_id,
            managed_thread_surface_key=managed_thread_surface_key,
        )
        await _deliver_discord_turn_result(
            dispatch,
            workspace_root=request.workspace_root,
            turn_result=turn_result,
        )

    progress_message_id = await _send_initial_progress_placeholder()
    if (
        progress_message_id is not None
        and isinstance(dispatch.event.message.message_id, str)
        and dispatch.event.message.message_id
    ):
        thread_target_id = await _resolve_managed_thread_id()
        if thread_target_id:
            _stash_discord_reusable_progress_message(
                dispatch.service,
                thread_target_id=thread_target_id,
                source_message_id=dispatch.event.message.message_id,
                channel_id=dispatch.channel_id,
                message_id=progress_message_id,
            )
    _spawn_discord_background_task(
        dispatch.service,
        _run_in_background(),
        await_on_shutdown=True,
    )
    return DiscordMessageTurnResult(
        final_message="",
        send_final_message=False,
        deferred_delivery=True,
    )


async def _execute_discord_thread_message(
    request: SurfaceThreadMessageRequest,
    *,
    dispatch: _DiscordMessageTurnDispatch,
    initial_progress_message_id: Optional[str] = None,
    managed_thread_surface_key: Optional[str] = None,
) -> DiscordMessageTurnResult:
    request_workspace_root = request.workspace_root
    prompt_text = dispatch.turn_text
    (
        prompt_text,
        saved_attachments,
        failed_attachments,
        transcript_message,
        attachment_input_items,
    ) = await dispatch.service._with_attachment_context(
        prompt_text=prompt_text,
        workspace_root=request_workspace_root,
        attachments=dispatch.event.attachments,
        channel_id=dispatch.channel_id,
    )
    if transcript_message:
        await dispatch.service._send_channel_message_safe(
            dispatch.channel_id,
            {
                "content": transcript_message,
                "allowed_mentions": {"parse": []},
            },
        )
    if failed_attachments > 0:
        await dispatch.service._send_channel_message_safe(
            dispatch.channel_id,
            {
                "content": (
                    "Some Discord attachments could not be downloaded. "
                    "Continuing with available inputs."
                )
            },
        )
    if not prompt_text.strip():
        if dispatch.has_attachments and saved_attachments == 0:
            await dispatch.service._send_channel_message_safe(
                dispatch.channel_id,
                {
                    "content": (
                        "Failed to download attachments from Discord. Please retry."
                    ),
                },
            )
        return DiscordMessageTurnResult(
            final_message="",
            preview_message_id=initial_progress_message_id,
            send_final_message=False,
        )

    if not dispatch.effective_pma_enabled:
        prompt_text, injected = maybe_inject_car_awareness(
            prompt_text,
            declared_profile="car_ambient",
        )
        if injected:
            dispatch.log_event_fn(
                dispatch.service._logger,
                logging.INFO,
                "discord.car_context.injected",
                channel_id=dispatch.channel_id,
                message_id=dispatch.event.message.message_id,
            )
        prompt_text, injected = maybe_inject_prompt_writing_hint(
            prompt_text,
            trigger_text=dispatch.text,
        )
        if injected:
            dispatch.log_event_fn(
                dispatch.service._logger,
                logging.INFO,
                "discord.prompt_context.injected",
                channel_id=dispatch.channel_id,
                message_id=dispatch.event.message.message_id,
            )
        prompt_text, injected = _maybe_inject_discord_filebox_hint(
            prompt_text,
            user_text=dispatch.text,
            workspace_root=request_workspace_root,
        )
        if injected:
            dispatch.log_event_fn(
                dispatch.service._logger,
                logging.INFO,
                "discord.filebox_context.injected",
                channel_id=dispatch.channel_id,
                message_id=dispatch.event.message.message_id,
            )

    if dispatch.effective_pma_enabled:
        try:
            snapshot = await build_hub_snapshot(
                dispatch.service._hub_supervisor, hub_root=dispatch.service._config.root
            )
            prompt_base = load_pma_prompt(dispatch.service._config.root)
            if dispatch.notification_reply is not None:
                prompt_text = (
                    f"{build_notification_context_block(dispatch.notification_reply)}\n\n"
                    f"{prompt_text}"
                )
            prompt_text = format_pma_prompt(
                prompt_base,
                snapshot,
                prompt_text,
                hub_root=dispatch.service._config.root,
                prompt_state_key=dispatch.session_key,
            )
        except (OSError, ValueError, KeyError, TypeError) as exc:
            dispatch.log_event_fn(
                dispatch.service._logger,
                logging.WARNING,
                "discord.pma.prompt_build.failed",
                channel_id=dispatch.channel_id,
                exc=exc,
            )
            await dispatch.service._send_channel_message_safe(
                dispatch.channel_id,
                {"content": "Failed to build PMA context. Please try again."},
            )
            return DiscordMessageTurnResult(
                final_message="",
                preview_message_id=initial_progress_message_id,
                send_final_message=False,
            )

    prompt_text, _github_injected = await dispatch.service._maybe_inject_github_context(
        prompt_text,
        request_workspace_root,
        link_source_text=dispatch.turn_text,
        allow_cross_repo=dispatch.pma_enabled,
    )
    if dispatch.pending_compact_seed:
        prompt_text = f"{dispatch.pending_compact_seed}\n\n{prompt_text}"

    turn_input_items: Optional[list[dict[str, Any]]] = None
    if attachment_input_items:
        turn_input_items = [
            {"type": "text", "text": prompt_text},
            *attachment_input_items,
        ]
    run_turn_kwargs: dict[str, Any] = {
        "workspace_root": request_workspace_root,
        "prompt_text": prompt_text,
        "agent": dispatch.agent,
        "model_override": dispatch.model_override,
        "reasoning_effort": dispatch.reasoning_effort,
        "session_key": dispatch.session_key,
        "orchestrator_channel_key": (
            dispatch.channel_id
            if not dispatch.effective_pma_enabled
            else f"pma:{dispatch.channel_id}"
        ),
        "source_message_id": dispatch.event.message.message_id,
    }
    resolved_managed_thread_surface_key = (
        managed_thread_surface_key
        or _managed_thread_surface_key_for_notification_reply(
            dispatch.notification_reply
        )
    )
    if resolved_managed_thread_surface_key is not None:
        run_turn_kwargs["managed_thread_surface_key"] = (
            resolved_managed_thread_surface_key
        )
    if turn_input_items:
        run_turn_kwargs["input_items"] = turn_input_items
    try:
        return cast(
            DiscordMessageTurnResult,
            await dispatch.service._run_agent_turn_for_message(**run_turn_kwargs),
        )
    except DiscordTurnStartupFailure as exc:
        dispatch.log_event_fn(
            dispatch.service._logger,
            logging.INFO,
            "discord.turn.startup_failed",
            channel_id=dispatch.channel_id,
            conversation_id=dispatch.context.conversation_id,
            workspace_root=str(dispatch.workspace_root),
            agent=dispatch.agent,
            exc=exc,
        )
        return DiscordMessageTurnResult(
            final_message="",
            preview_message_id=None,
            send_final_message=False,
        )
    except (
        RuntimeError,
        ConnectionError,
        OSError,
        ValueError,
        TypeError,
        KeyError,
        TimeoutError,
        AttributeError,
    ) as exc:
        dispatch.log_event_fn(
            dispatch.service._logger,
            logging.WARNING,
            "discord.turn.failed",
            channel_id=dispatch.channel_id,
            conversation_id=dispatch.context.conversation_id,
            workspace_root=str(dispatch.workspace_root),
            agent=dispatch.agent,
            exc=exc,
        )
        await dispatch.service._send_channel_message_safe(
            dispatch.channel_id,
            {
                "content": (
                    f"Turn failed: {exc} (conversation {dispatch.context.conversation_id})"
                )
            },
        )
        return DiscordMessageTurnResult(
            final_message="",
            preview_message_id=initial_progress_message_id,
            send_final_message=False,
        )


async def _handle_discord_notification_turn(
    dispatch: _DiscordMessageTurnDispatch,
) -> DiscordMessageTurnResult:
    notification_workspace_root = dispatch.workspace_root
    stored_workspace_root = getattr(dispatch.notification_reply, "workspace_root", None)
    if isinstance(stored_workspace_root, str) and stored_workspace_root.strip():
        notification_workspace_root = Path(stored_workspace_root)
    turn_result = await _submit_discord_thread_message(
        dispatch.build_request(
            workspace_root=notification_workspace_root,
            pma_enabled=True,
        ),
        dispatch=dispatch,
    )
    surface_key = notification_surface_key(dispatch.notification_reply.notification_id)
    orch_binding = build_discord_thread_orchestration_service(
        dispatch.service
    ).get_binding(
        surface_kind="discord",
        surface_key=surface_key,
    )
    if orch_binding is not None:
        PmaNotificationStore(dispatch.service._config.root).bind_continuation_thread(
            notification_id=dispatch.notification_reply.notification_id,
            thread_target_id=orch_binding.thread_target_id,
        )
    return turn_result


async def _deliver_discord_turn_result(
    dispatch: _DiscordMessageTurnDispatch,
    *,
    workspace_root: Path,
    turn_result: Any,
) -> None:
    if isinstance(turn_result, DiscordMessageTurnResult):
        if turn_result.deferred_delivery:
            return
        response_text = turn_result.final_message
        preview_message_id = turn_result.preview_message_id
        send_final_message = turn_result.send_final_message
        intermediate_text = (
            turn_result.intermediate_message.strip()
            if isinstance(turn_result.intermediate_message, str)
            else ""
        )
        response_text = compose_turn_response_with_footer(
            response_text,
            summary_text=intermediate_text,
            token_usage=turn_result.token_usage,
            elapsed_seconds=turn_result.elapsed_seconds,
            agent=dispatch.agent,
            model=dispatch.model_override,
        )
    else:
        response_text = str(turn_result or "")
        preview_message_id = None
        send_final_message = True

    if isinstance(preview_message_id, str) and preview_message_id:
        await dispatch.service._delete_channel_message_safe(
            channel_id=dispatch.channel_id,
            message_id=preview_message_id,
            record_id=(
                f"turn:delete_progress:{dispatch.session_key}:{uuid.uuid4().hex[:8]}"
            ),
        )
    if send_final_message:
        await _send_discord_turn_section(
            dispatch.service,
            channel_id=dispatch.channel_id,
            text=response_text or "(No response text returned.)",
            record_prefix=f"turn:final:{dispatch.session_key}",
            attachment_filename="final-response.md",
            attachment_caption="Final response too long; attached as final-response.md.",
        )
    if dispatch.pending_compact_seed is not None:
        await dispatch.service._store.clear_pending_compact_seed(
            channel_id=dispatch.channel_id
        )
    if send_final_message:
        await dispatch.service._flush_outbox_files(
            workspace_root=workspace_root,
            channel_id=dispatch.channel_id,
        )


async def handle_message_event(
    service: Any,
    event: ChatMessageEvent,
    context: DispatchContext,
    *,
    channel_id: str,
    text: str,
    has_attachments: bool,
    policy_result: Optional[CollaborationEvaluationResult] = None,
    log_event_fn: Any,
    build_ticket_flow_controller_fn: Any,
    ensure_worker_fn: Any,
) -> None:
    turn_text = compose_inbound_message_text(
        text,
        forwarded_from=event.forwarded_from,
        reply_context=event.reply_context,
    )
    flow_reply_text = compose_forwarded_message_text(text, event.forwarded_from)
    binding, workspace_root = await resolve_bound_workspace_root(
        service,
        channel_id=channel_id,
    )
    if binding is None:
        log_event_fn(
            service._logger,
            logging.INFO,
            "discord.message.unbound_plain_text_ignored",
            channel_id=channel_id,
            guild_id=context.thread_id,
            user_id=event.from_user_id,
            message_id=event.message.message_id,
            **(policy_result.log_fields() if policy_result is not None else {}),
        )
        return

    pma_enabled = bool(binding.get("pma_enabled", False))
    if workspace_root is None:
        content = format_discord_message(
            "Binding is invalid. Run `/car bind path:<workspace>`."
        )
        await service._send_channel_message_safe(
            channel_id,
            {"content": content},
        )
        return

    notification_reply = None
    if event.reply_to is not None:
        notification_reply = PmaNotificationStore(
            service._config.root
        ).get_reply_target(
            surface_kind="discord",
            surface_key=channel_id,
            delivered_message_id=event.reply_to.message_id,
        )
    effective_pma_enabled = pma_enabled or notification_reply is not None
    agent, agent_profile = service._resolve_agent_state(binding)
    runtime_agent = service._runtime_agent_for_binding(binding)
    model_override = binding.get("model_override")
    if not isinstance(model_override, str) or not model_override.strip():
        model_override = None
    reasoning_effort = binding.get("reasoning_effort")
    if not isinstance(reasoning_effort, str) or not reasoning_effort.strip():
        reasoning_effort = None
    session_key = service._build_message_session_key(
        channel_id=channel_id,
        workspace_root=workspace_root,
        pma_enabled=effective_pma_enabled,
        agent=agent,
        agent_profile=agent_profile,
    )
    pending_compact_seed = match_pending_compact_seed(
        binding.get("pending_compact_seed"),
        pending_target_id=binding.get("pending_compact_session_key"),
        active_target_id=session_key,
    )
    dispatch = _DiscordMessageTurnDispatch(
        service=service,
        event=event,
        context=context,
        binding=binding,
        channel_id=channel_id,
        text=text,
        has_attachments=has_attachments,
        log_event_fn=log_event_fn,
        build_ticket_flow_controller_fn=build_ticket_flow_controller_fn,
        ensure_worker_fn=ensure_worker_fn,
        workspace_root=workspace_root,
        pma_enabled=pma_enabled,
        effective_pma_enabled=effective_pma_enabled,
        notification_reply=notification_reply,
        agent=agent,
        agent_profile=agent_profile,
        runtime_agent=runtime_agent,
        model_override=model_override,
        reasoning_effort=reasoning_effort,
        session_key=session_key,
        pending_compact_seed=pending_compact_seed,
        turn_text=turn_text,
        flow_reply_text=flow_reply_text,
    )
    ingress = _build_discord_surface_ingress(dispatch)

    if notification_reply is not None:
        turn_result = await _handle_discord_notification_turn(dispatch)
    else:
        result = await ingress.submit_message(
            dispatch.build_request(pma_enabled=pma_enabled),
            resolve_paused_flow_target=partial(
                _resolve_discord_paused_flow, dispatch=dispatch
            ),
            submit_flow_reply=partial(_submit_discord_flow_reply, dispatch=dispatch),
            submit_thread_message=partial(
                _submit_discord_thread_message, dispatch=dispatch
            ),
        )
        if result.route == "flow":
            return
        turn_result = result.thread_result

    await _deliver_discord_turn_result(
        dispatch,
        workspace_root=workspace_root,
        turn_result=turn_result,
    )


async def run_agent_turn_for_message(
    service: Any,
    *,
    workspace_root: Path,
    prompt_text: str,
    input_items: Optional[list[dict[str, Any]]] = None,
    managed_thread_surface_key: Optional[str] = None,
    source_message_id: Optional[str] = None,
    agent: str,
    model_override: Optional[str],
    reasoning_effort: Optional[str],
    session_key: str,
    orchestrator_channel_key: str,
    max_actions: int,
    min_edit_interval_seconds: float,
    heartbeat_interval_seconds: float,
    log_event_fn: Any,
) -> DiscordMessageTurnResult:
    _ = (
        max_actions,
        min_edit_interval_seconds,
        heartbeat_interval_seconds,
        log_event_fn,
    )
    binding = await service._store.get_binding(channel_id=orchestrator_channel_key)
    approval_mode, sandbox_policy = _resolve_discord_turn_policies(
        binding,
        default_approval_policy="never",
        default_sandbox_policy="dangerFullAccess",
    )
    return await _run_discord_orchestrated_turn_for_message(
        service,
        workspace_root=workspace_root,
        prompt_text=prompt_text,
        input_items=input_items,
        managed_thread_surface_key=managed_thread_surface_key,
        source_message_id=source_message_id,
        agent=agent,
        model_override=model_override,
        reasoning_effort=reasoning_effort,
        session_key=session_key,
        orchestrator_channel_key=orchestrator_channel_key,
        mode="repo",
        pma_enabled=False,
        execution_prompt=prompt_text,
        public_execution_error=DISCORD_REPO_PUBLIC_EXECUTION_ERROR,
        timeout_error="Discord turn timed out",
        interrupted_error="Discord turn interrupted",
        approval_mode=approval_mode,
        sandbox_policy=sandbox_policy,
        max_actions=max_actions,
        min_edit_interval_seconds=min_edit_interval_seconds,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
    )


def _build_managed_thread_input_items(
    runtime_prompt: str,
    input_items: Optional[list[dict[str, Any]]],
) -> Optional[list[dict[str, Any]]]:
    return _shared_build_managed_thread_input_items(
        runtime_prompt,
        input_items,
    )


def build_discord_thread_orchestration_service(service: Any) -> Any:
    cached = getattr(service, "_discord_thread_orchestration_service", None)
    if cached is None:
        cached = getattr(service, "_discord_managed_thread_orchestration_service", None)
    if cached is not None:
        return cached

    descriptors = get_registered_agents(service)

    def _make_harness(agent_id: str, profile: Optional[str] = None) -> Any:
        descriptor = descriptors.get(agent_id)
        if descriptor is None:
            raise KeyError(f"Unknown agent definition '{agent_id}'")
        return descriptor.make_harness(
            wrap_requested_agent_context(service, agent_id=agent_id, profile=profile)
        )

    created = build_harness_backed_orchestration_service(
        descriptors=cast(Any, descriptors),
        harness_factory=_make_harness,
        pma_thread_store=PmaThreadStore(service._config.root),
    )
    service._discord_thread_orchestration_service = created
    service._discord_managed_thread_orchestration_service = created
    return created


def resolve_discord_thread_target(
    service: Any,
    *,
    channel_id: str,
    managed_thread_surface_key: Optional[str] = None,
    workspace_root: Path,
    agent: str,
    agent_profile: Optional[str] = None,
    repo_id: Optional[str],
    resource_kind: Optional[str],
    resource_id: Optional[str],
    mode: str,
    pma_enabled: bool,
) -> Any:
    orchestration_service = build_discord_thread_orchestration_service(service)
    surface_key = managed_thread_surface_key or channel_id
    runtime_agent = resolve_chat_runtime_agent(
        agent,
        agent_profile,
        default=getattr(service, "DEFAULT_AGENT", "codex"),
        context=service,
    )
    owner_kind, owner_id, normalized_repo_id = service._resource_owner_for_workspace(
        workspace_root,
        repo_id=repo_id,
        resource_kind=resource_kind,
        resource_id=resource_id,
    )
    return _shared_resolve_managed_thread_target(
        orchestration_service,
        request=ManagedThreadTargetRequest(
            surface_kind="discord",
            surface_key=surface_key,
            mode=mode,
            agent=agent,
            agent_profile=agent_profile,
            workspace_root=workspace_root,
            display_name=f"discord:{surface_key}",
            repo_id=normalized_repo_id,
            resource_kind=owner_kind,
            resource_id=owner_id,
            binding_metadata={"channel_id": channel_id, "pma_enabled": pma_enabled},
            reusable_agent_ids=(runtime_agent,),
        ),
    )


@dataclass(frozen=True)
class _DiscordManagedThreadStatus:
    thread_target_id: Optional[str]
    busy: bool


def _resolve_discord_managed_thread_status(
    dispatch: _DiscordMessageTurnDispatch,
    *,
    workspace_root: Path,
    managed_thread_surface_key: Optional[str],
    pma_enabled: bool,
) -> _DiscordManagedThreadStatus:
    orchestration_service = build_discord_thread_orchestration_service(dispatch.service)
    surface_key = managed_thread_surface_key or dispatch.channel_id
    get_binding = getattr(orchestration_service, "get_binding", None)
    get_thread_target = getattr(orchestration_service, "get_thread_target", None)
    if not callable(get_binding) or not callable(get_thread_target):
        return _DiscordManagedThreadStatus(thread_target_id=None, busy=False)
    try:
        binding = get_binding(surface_kind="discord", surface_key=surface_key)
    except (RuntimeError, ValueError, TypeError, KeyError, AttributeError):
        return _DiscordManagedThreadStatus(thread_target_id=None, busy=False)
    normalized_mode = "pma" if pma_enabled else "repo"
    if str(getattr(binding, "mode", "") or "").strip().lower() != normalized_mode:
        return _DiscordManagedThreadStatus(thread_target_id=None, busy=False)
    thread_target_id = (
        str(getattr(binding, "thread_target_id", "") or "").strip() or None
    )
    if not thread_target_id:
        return _DiscordManagedThreadStatus(thread_target_id=None, busy=False)
    try:
        thread = get_thread_target(thread_target_id)
    except (RuntimeError, ValueError, TypeError, KeyError, AttributeError):
        return _DiscordManagedThreadStatus(thread_target_id=None, busy=False)
    canonical_workspace = str(canonicalize_path(workspace_root))
    if str(getattr(thread, "workspace_root", "") or "").strip() != canonical_workspace:
        return _DiscordManagedThreadStatus(thread_target_id=None, busy=False)

    busy = False
    get_running_execution = getattr(
        orchestration_service, "get_running_execution", None
    )
    if callable(get_running_execution):
        with contextlib.suppress(RuntimeError, ValueError, TypeError, AttributeError):
            busy = get_running_execution(thread_target_id) is not None
    if not busy:
        list_queued_executions = getattr(
            orchestration_service, "list_queued_executions", None
        )
        if callable(list_queued_executions):
            with contextlib.suppress(
                RuntimeError, ValueError, TypeError, AttributeError
            ):
                busy = bool(list_queued_executions(thread_target_id, limit=1))
    return _DiscordManagedThreadStatus(thread_target_id=thread_target_id, busy=busy)


def _build_discord_managed_thread_coordinator(
    *,
    service: Any,
    orchestration_service: Any,
    channel_id: str,
    public_execution_error: str,
    timeout_error: str,
    interrupted_error: str,
) -> ManagedThreadTurnCoordinator:
    return ManagedThreadTurnCoordinator(
        orchestration_service=orchestration_service,
        state_root=service._config.root,
        surface=ManagedThreadSurfaceInfo(
            log_label="Discord",
            surface_kind="discord",
            surface_key=channel_id,
        ),
        errors=ManagedThreadErrorMessages(
            public_execution_error=public_execution_error,
            timeout_error=timeout_error,
            interrupted_error=interrupted_error,
            timeout_seconds=DISCORD_PMA_TIMEOUT_SECONDS,
        ),
        logger=getattr(service, "_logger", _logger),
        turn_preview="",
        preview_builder=lambda message_text: truncate_for_discord(
            message_text,
            max_len=120,
        ),
    )


def _get_discord_thread_queue_task_map(service: Any) -> dict[str, asyncio.Task[Any]]:
    return _discord_orchestration_state(service).thread_queue_tasks


def _build_discord_queue_worker_hooks(
    service: Any,
    *,
    channel_id: str,
    managed_thread_id: str,
    public_execution_error: str,
) -> ManagedThreadQueueWorkerHooks:
    async def _run_with_discord_typing_indicator(work: Any) -> None:
        run_with_typing = getattr(service, "_run_with_typing_indicator", None)
        if callable(run_with_typing):
            await run_with_typing(channel_id=channel_id, work=work)
            return
        await work()

    async def _on_execution_started(
        started_execution: RuntimeThreadExecution,
    ) -> None:
        service._register_discord_turn_approval_context(
            started_execution=started_execution,
            channel_id=channel_id,
        )

    def _on_execution_finished(started_execution: RuntimeThreadExecution) -> None:
        service._clear_discord_turn_approval_context(
            started_execution=started_execution
        )

    async def _deliver_result(finalized: ManagedThreadFinalizationResult) -> None:
        if finalized.status == "ok":
            assistant_text = finalized.assistant_text.strip()
            message = (
                format_discord_message(assistant_text)
                if assistant_text
                else "(No response text returned.)"
            )
            await service._send_channel_message_safe(
                channel_id,
                {"content": message},
                record_id=(
                    f"discord-queued:{managed_thread_id}:{finalized.managed_turn_id}"
                ),
            )
            return
        await service._send_channel_message_safe(
            channel_id,
            {"content": (f"Turn failed: {finalized.error or public_execution_error}")},
            record_id=(
                f"discord-queued-error:{managed_thread_id}:{finalized.managed_turn_id}"
            ),
        )

    return ManagedThreadQueueWorkerHooks(
        deliver_result=_deliver_result,
        run_with_indicator=_run_with_discord_typing_indicator,
        execution_hooks=ManagedThreadExecutionHooks(
            on_execution_started=_on_execution_started,
            on_execution_finished=_on_execution_finished,
        ),
    )


async def _run_discord_orchestrated_turn_for_message(
    service: Any,
    *,
    workspace_root: Path,
    prompt_text: str,
    input_items: Optional[list[dict[str, Any]]] = None,
    source_message_id: Optional[str] = None,
    agent: str,
    model_override: Optional[str],
    reasoning_effort: Optional[str],
    session_key: str,
    orchestrator_channel_key: str,
    managed_thread_surface_key: Optional[str],
    mode: str,
    pma_enabled: bool,
    execution_prompt: str,
    public_execution_error: str,
    timeout_error: str,
    interrupted_error: str,
    approval_mode: str,
    sandbox_policy: str,
    max_actions: int,
    min_edit_interval_seconds: float,
    heartbeat_interval_seconds: float,
) -> DiscordMessageTurnResult:
    _ = session_key
    channel_id = (
        orchestrator_channel_key.split(":", 1)[1]
        if pma_enabled and ":" in orchestrator_channel_key
        else orchestrator_channel_key
    )
    binding = await service._store.get_binding(channel_id=channel_id)
    logical_agent, agent_profile = service._resolve_agent_state(binding)
    if not isinstance(logical_agent, str) or not logical_agent.strip():
        logical_agent = agent
    repo_id = binding.get("repo_id") if isinstance(binding, dict) else None
    resource_kind = binding.get("resource_kind") if isinstance(binding, dict) else None
    resource_id = binding.get("resource_id") if isinstance(binding, dict) else None
    orchestration_service, thread = resolve_discord_thread_target(
        service,
        channel_id=channel_id,
        managed_thread_surface_key=managed_thread_surface_key,
        workspace_root=workspace_root,
        agent=logical_agent,
        agent_profile=agent_profile,
        repo_id=repo_id if isinstance(repo_id, str) and repo_id.strip() else None,
        resource_kind=(
            resource_kind.strip()
            if isinstance(resource_kind, str) and resource_kind.strip()
            else None
        ),
        resource_id=(
            resource_id.strip()
            if isinstance(resource_id, str) and resource_id.strip()
            else None
        ),
        mode=mode,
        pma_enabled=pma_enabled,
    )
    execution_input_items = _build_managed_thread_input_items(
        execution_prompt,
        input_items,
    )
    max_progress_len = max(int(service._config.max_message_length), 32)
    managed_thread_id = thread.thread_target_id
    coordinator = _build_discord_managed_thread_coordinator(
        service=service,
        orchestration_service=orchestration_service,
        channel_id=channel_id,
        public_execution_error=public_execution_error,
        timeout_error=timeout_error,
        interrupted_error=interrupted_error,
    )
    tracker = TurnProgressTracker(
        started_at=time.monotonic(),
        agent=agent,
        model=model_override or "default",
        label="working",
        max_actions=max_actions,
        max_output_chars=max_progress_len,
    )
    progress_message_id: Optional[str] = None
    progress_rendered: Optional[str] = None
    progress_last_updated = 0.0
    progress_heartbeat_task: Optional[asyncio.Task[None]] = None
    progress_execution_id: Optional[str] = None
    runtime_state = ProgressRuntimeState()
    active_progress_labels = {"working", "queued", "running", "review"}
    reusable_progress_message_id = _claim_discord_reusable_progress_message(
        service,
        thread_target_id=managed_thread_id,
        source_message_id=source_message_id,
    )

    async def _edit_progress(
        *,
        force: bool = False,
        remove_components: bool = False,
        render_mode: str = "live",
    ) -> None:
        nonlocal progress_rendered
        nonlocal progress_last_updated
        if not progress_message_id:
            return
        now = time.monotonic()
        if not force and (now - progress_last_updated) < min_edit_interval_seconds:
            return
        rendered = render_progress_text(
            tracker,
            max_length=max_progress_len,
            now=now,
            render_mode=render_mode,
        )
        content = truncate_for_discord(rendered, max_len=max_progress_len)
        if not force and content == progress_rendered:
            return
        payload: dict[str, Any] = {"content": content}
        if remove_components:
            payload["components"] = []
        elif tracker.label in active_progress_labels:
            payload["components"] = [
                build_cancel_turn_button(
                    custom_id=build_cancel_turn_custom_id(
                        thread_target_id=managed_thread_id,
                        execution_id=progress_execution_id,
                    )
                )
            ]
        else:
            payload["components"] = []
        try:
            await service._rest.edit_channel_message(
                channel_id=channel_id,
                message_id=progress_message_id,
                payload=payload,
            )
        except (RuntimeError, ConnectionError, OSError):
            _logger.debug(
                "Discord progress edit failed for message=%s",
                progress_message_id,
                exc_info=True,
            )
            progress_last_updated = now
            return
        progress_rendered = content
        progress_last_updated = now

    async def _progress_heartbeat() -> None:
        while True:
            await asyncio.sleep(heartbeat_interval_seconds)
            await _edit_progress()

    async def _stop_progress_heartbeat() -> None:
        nonlocal progress_heartbeat_task
        if progress_heartbeat_task is not None:
            progress_heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await progress_heartbeat_task
            progress_heartbeat_task = None

    async def _begin_next_execution(
        orchestration_service: Any,
        queued_managed_thread_id: str,
    ) -> Optional[RuntimeThreadExecution]:
        return await begin_next_queued_runtime_thread_execution(
            orchestration_service,
            queued_managed_thread_id,
        )

    async def _begin_execution(
        orchestration_service: Any,
        request: MessageRequest,
        *,
        client_request_id: Optional[str],
        sandbox_policy: Optional[Any],
    ) -> RuntimeThreadExecution:
        return await begin_runtime_thread_execution(
            orchestration_service,
            request,
            client_request_id=client_request_id,
            sandbox_policy=sandbox_policy,
        )

    def ensure_queue_worker() -> None:
        coordinator.ensure_queue_worker(
            task_map=_get_discord_thread_queue_task_map(service),
            managed_thread_id=managed_thread_id,
            spawn_task=lambda coro: _spawn_discord_background_task(
                service, coro, await_on_shutdown=True
            ),
            hooks=_build_discord_queue_worker_hooks(
                service,
                channel_id=channel_id,
                managed_thread_id=managed_thread_id,
                public_execution_error=public_execution_error,
            ),
            begin_next_execution=cast(
                ManagedThreadQueuedExecutionStarter,
                _begin_next_execution,
            ),
        )

    try:
        if reusable_progress_message_id:
            progress_message_id = reusable_progress_message_id
            await _edit_progress(force=True)
            progress_heartbeat_task = asyncio.create_task(_progress_heartbeat())
        else:
            initial_rendered = render_progress_text(
                tracker,
                max_length=max_progress_len,
                now=time.monotonic(),
            )
            initial_content = truncate_for_discord(
                initial_rendered,
                max_len=max_progress_len,
            )
            response = await service._send_channel_message(
                channel_id,
                {
                    "content": initial_content,
                    "components": [
                        build_cancel_turn_button(
                            custom_id=build_cancel_turn_custom_id(
                                thread_target_id=managed_thread_id,
                            )
                        )
                    ],
                },
            )
            message_id = response.get("id")
            if isinstance(message_id, str) and message_id:
                progress_message_id = message_id
                progress_rendered = initial_content
                progress_last_updated = time.monotonic()
                progress_heartbeat_task = asyncio.create_task(_progress_heartbeat())
    except (RuntimeError, ConnectionError, OSError):
        service._logger.warning(
            "Discord progress placeholder send failed for channel=%s",
            channel_id,
            exc_info=True,
        )
        progress_message_id = None

    try:
        submission = await asyncio.wait_for(
            coordinator.submit_execution(
                MessageRequest(
                    target_id=thread.thread_target_id,
                    target_kind="thread",
                    message_text=prompt_text,
                    busy_policy="queue",
                    model=model_override,
                    reasoning=reasoning_effort,
                    approval_mode=approval_mode,
                    input_items=execution_input_items,
                    metadata={
                        "runtime_prompt": execution_prompt,
                        "execution_error_message": public_execution_error,
                    },
                ),
                client_request_id=f"discord:{channel_id}:{uuid.uuid4().hex[:12]}",
                sandbox_policy=sandbox_policy,
                begin_execution=_begin_execution,
            ),
            timeout=DISCORD_MANAGED_THREAD_SUBMISSION_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError as exc:
        log_event(
            service._logger,
            logging.ERROR,
            "discord.turn.submission_timeout",
            channel_id=channel_id,
            thread_target_id=managed_thread_id,
            timeout_seconds=DISCORD_MANAGED_THREAD_SUBMISSION_TIMEOUT_SECONDS,
            pma_enabled=pma_enabled,
            workspace_root=str(workspace_root),
            agent=logical_agent,
        )
        await _stop_progress_heartbeat()
        tracker.set_label("failed")
        tracker.note_error("Turn failed to start in time. Please retry.")
        if progress_message_id:
            await _edit_progress(
                force=True,
                remove_components=True,
            )
        else:
            await service._send_channel_message_safe(
                channel_id,
                {"content": ("Turn failed to start in time. Please retry.")},
                record_id=(
                    f"discord:runtime-submit-timeout:{managed_thread_id}:"
                    f"{uuid.uuid4().hex[:8]}"
                ),
            )
        raise DiscordTurnStartupFailure(
            "Turn failed to start in time. Please retry."
        ) from exc
    except (RuntimeError, ConnectionError, OSError, ValueError, TypeError):
        await _stop_progress_heartbeat()
        if progress_message_id:
            await service._delete_channel_message_safe(
                channel_id,
                progress_message_id,
                record_id=(
                    f"discord:runtime-begin-failed:{managed_thread_id}:"
                    f"{uuid.uuid4().hex[:8]}"
                ),
            )
        raise
    started_execution = submission.started_execution

    progress_execution_id = (
        str(getattr(started_execution.execution, "execution_id", "") or "").strip()
        or None
    )
    if progress_message_id:
        try:
            await _edit_progress(force=True)
        except (RuntimeError, ConnectionError, OSError):
            _logger.debug(
                "Discord progress cancel-button refresh failed for channel=%s",
                channel_id,
                exc_info=True,
            )

    if submission.queued:
        await _stop_progress_heartbeat()
        tracker.set_label("queued")
        try:
            if progress_message_id:
                await _edit_progress(force=True)
        except (RuntimeError, ConnectionError, OSError):
            _logger.debug(
                "Discord queued-state progress edit failed for channel=%s",
                channel_id,
                exc_info=True,
            )
        ensure_queue_worker()
        return DiscordMessageTurnResult(
            final_message="Queued (waiting for available worker...)"
        )

    try:
        finalized_flow = await complete_managed_thread_execution(
            coordinator,
            submission,
            ensure_queue_worker=ensure_queue_worker,
            direct_hooks=ManagedThreadExecutionHooks(
                on_execution_started=(
                    lambda active_execution: (
                        service._register_discord_turn_approval_context(
                            started_execution=active_execution,
                            channel_id=channel_id,
                        )
                    )
                ),
                on_execution_finished=(
                    lambda active_execution: (
                        service._clear_discord_turn_approval_context(
                            started_execution=active_execution
                        )
                    )
                ),
                on_progress_event=lambda run_event: _apply_discord_progress_run_event(
                    tracker,
                    run_event,
                    runtime_state=runtime_state,
                    edit_progress=_edit_progress,
                ),
            ),
            runtime_event_state=RuntimeThreadRunEventState(),
        )
    finally:
        await _stop_progress_heartbeat()

    finalized = coerce_managed_thread_finalization_result(finalized_flow.finalized)
    assert finalized is not None
    if finalized.status != "ok":
        if finalized.status == "interrupted":
            reuse_request = _peek_discord_progress_reuse_request(
                service,
                thread_target_id=managed_thread_id,
            )
            if reuse_request is not None:
                acknowledgement_delivered = False
                if progress_message_id:
                    acknowledgement_delivered = (
                        await _acknowledge_discord_progress_reuse(
                            service,
                            channel_id=channel_id,
                            message_id=progress_message_id,
                            acknowledgement=reuse_request.acknowledgement,
                        )
                    )
                    if acknowledgement_delivered:
                        _stash_discord_reusable_progress_message(
                            service,
                            thread_target_id=managed_thread_id,
                            source_message_id=reuse_request.source_message_id,
                            channel_id=channel_id,
                            message_id=progress_message_id,
                        )
                if not acknowledgement_delivered:
                    clear_discord_turn_progress_reuse(
                        service,
                        thread_target_id=managed_thread_id,
                    )
                return DiscordMessageTurnResult(
                    final_message=sanitize_discord_outbound_text(
                        reuse_request.acknowledgement
                    ),
                    send_final_message=not acknowledgement_delivered,
                )
        raise RuntimeError(str(finalized.error or public_execution_error))
    summary_snapshot = render_progress_text(
        tracker,
        max_length=max_progress_len,
        now=time.monotonic(),
        render_mode="live",
    )
    intermediate_message = (
        summary_snapshot.splitlines()[0].strip() if summary_snapshot else ""
    )
    if not intermediate_message:
        intermediate_message = render_progress_text(
            tracker,
            max_length=max_progress_len,
            now=time.monotonic(),
            render_mode="final",
        )
    return DiscordMessageTurnResult(
        final_message=finalized.assistant_text,
        preview_message_id=progress_message_id,
        intermediate_message=intermediate_message,
        token_usage=finalized.token_usage,
        elapsed_seconds=max(0.0, time.monotonic() - tracker.started_at),
    )


async def _send_discord_turn_section(
    service: Any,
    *,
    channel_id: str,
    text: str,
    record_prefix: str,
    attachment_filename: str,
    attachment_caption: str,
) -> None:
    chunks = chunk_discord_message(
        text,
        max_len=service._config.max_message_length,
        with_numbering=False,
    )
    if (
        service._config.message_overflow == "document"
        and len(chunks) > 3
        and text.strip()
    ):
        try:
            await service._rest.create_channel_message_with_attachment(
                channel_id=channel_id,
                data=text.encode("utf-8"),
                filename=attachment_filename,
                caption=attachment_caption,
            )
            return
        except (ConnectionError, OSError, TimeoutError):
            _logger.debug(
                "attachment upload failed, falling back to chunks", exc_info=True
            )
    if not chunks:
        chunks = ["(No response text returned.)"]
    for idx, chunk in enumerate(chunks, 1):
        await service._send_channel_message_safe(
            channel_id,
            {"content": chunk},
            record_id=f"{record_prefix}:{idx}:{uuid.uuid4().hex[:8]}",
        )


async def run_managed_thread_turn_for_message(
    service: Any,
    *,
    workspace_root: Path,
    prompt_text: str,
    input_items: Optional[list[dict[str, Any]]] = None,
    source_message_id: Optional[str] = None,
    agent: str,
    model_override: Optional[str],
    reasoning_effort: Optional[str],
    session_key: str,
    orchestrator_channel_key: str,
    managed_thread_surface_key: Optional[str] = None,
) -> DiscordMessageTurnResult:
    execution_prompt = (
        f"{format_pma_discoverability_preamble(hub_root=service._config.root)}"
        "<user_message>\n"
        f"{prompt_text}\n"
        "</user_message>\n"
    )
    binding = await service._store.get_binding(channel_id=orchestrator_channel_key)
    approval_mode, sandbox_policy = _resolve_discord_turn_policies(
        binding,
        default_approval_policy="never",
        default_sandbox_policy="dangerFullAccess",
    )
    return await _run_discord_orchestrated_turn_for_message(
        service,
        workspace_root=workspace_root,
        prompt_text=prompt_text,
        input_items=input_items,
        source_message_id=source_message_id,
        agent=agent,
        model_override=model_override,
        reasoning_effort=reasoning_effort,
        session_key=session_key,
        orchestrator_channel_key=orchestrator_channel_key,
        managed_thread_surface_key=managed_thread_surface_key,
        mode="pma",
        pma_enabled=True,
        execution_prompt=execution_prompt,
        public_execution_error=DISCORD_PMA_PUBLIC_EXECUTION_ERROR,
        timeout_error="Discord PMA turn timed out",
        interrupted_error="Discord PMA turn interrupted",
        approval_mode=approval_mode,
        sandbox_policy=sandbox_policy,
        max_actions=DISCORD_PMA_PROGRESS_MAX_ACTIONS,
        min_edit_interval_seconds=DISCORD_PMA_PROGRESS_MIN_EDIT_INTERVAL_SECONDS,
        heartbeat_interval_seconds=DISCORD_PMA_PROGRESS_HEARTBEAT_INTERVAL_SECONDS,
    )
