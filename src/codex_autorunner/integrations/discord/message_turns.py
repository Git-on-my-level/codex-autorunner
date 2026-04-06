from __future__ import annotations

import asyncio
import contextlib
import functools
import inspect
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, cast

from ...agents.base import (
    harness_progress_event_stream,
    harness_supports_progress_event_stream,
)
from ...agents.registry import get_registered_agents, wrap_requested_agent_context
from ...core.context_awareness import (
    maybe_inject_car_awareness,
    maybe_inject_filebox_hint,
    maybe_inject_prompt_writing_hint,
)
from ...core.filebox import inbox_dir, outbox_dir, outbox_pending_dir
from ...core.injected_context import wrap_injected_context
from ...core.orchestration import (
    FlowTarget,
    MessageRequest,
    PausedFlowTarget,
    SurfaceThreadMessageRequest,
    build_harness_backed_orchestration_service,
    build_surface_orchestration_ingress,
)
from ...core.orchestration.runtime_thread_events import (
    RuntimeThreadRunEventState,
    normalize_runtime_thread_raw_event,
    recover_post_completion_outcome,
    terminal_run_event_from_outcome,
)
from ...core.orchestration.runtime_threads import (
    RuntimeThreadExecution,
    RuntimeThreadOutcome,
    await_runtime_thread_outcome,
    begin_next_queued_runtime_thread_execution,
    begin_runtime_thread_execution,
)
from ...core.orchestration.turn_timeline import persist_turn_timeline
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
from ...core.pma_transcripts import PmaTranscriptStore
from ...core.ports.run_event import (
    RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
    RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
    ApprovalRequested,
    Completed,
    Failed,
    OutputDelta,
    RunNotice,
    Started,
    TokenUsage,
    ToolCall,
)
from ...core.utils import canonicalize_path
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
    resolve_runtime_thread_error_detail as _resolve_runtime_thread_result_error_detail,
)
from ...integrations.chat.runtime_thread_errors import (
    sanitize_runtime_thread_error,
)
from ..chat.managed_thread_progress import (
    ProgressRuntimeState,
    apply_run_event_to_progress_tracker,
)
from ..chat.progress_primitives import TurnProgressTracker, render_progress_text
from ..chat.turn_metrics import (
    _extract_context_usage_percent,
    compose_turn_response_with_footer,
)
from .components import build_cancel_turn_button
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
DISCORD_PMA_PROGRESS_MAX_ACTIONS = 12
DISCORD_PMA_PROGRESS_MIN_EDIT_INTERVAL_SECONDS = 1.0
DISCORD_PMA_PROGRESS_HEARTBEAT_INTERVAL_SECONDS = 2.0


@dataclass(frozen=True)
class DiscordMessageTurnResult:
    final_message: str
    preview_message_id: Optional[str] = None
    intermediate_message: Optional[str] = None
    token_usage: Optional[dict[str, Any]] = None
    elapsed_seconds: Optional[float] = None
    send_final_message: bool = True


@dataclass(frozen=True)
class _DiscordProgressReuseRequest:
    source_message_id: str
    acknowledgement: str


@dataclass(frozen=True)
class _DiscordReusableProgressMessage:
    source_message_id: str
    channel_id: str
    message_id: str


_sanitize_runtime_thread_result_error = sanitize_runtime_thread_error


def _get_discord_progress_reuse_requests(
    service: Any,
) -> dict[str, _DiscordProgressReuseRequest]:
    requests = getattr(service, "_discord_turn_progress_reuse_requests", None)
    if not isinstance(requests, dict):
        requests = {}
        service._discord_turn_progress_reuse_requests = requests
    return requests


def _get_discord_reusable_progress_messages(
    service: Any,
) -> dict[str, _DiscordReusableProgressMessage]:
    messages = getattr(service, "_discord_reusable_progress_messages", None)
    if not isinstance(messages, dict):
        messages = {}
        service._discord_reusable_progress_messages = messages
    return messages


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
    if not isinstance(request, _DiscordProgressReuseRequest):
        return None
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


def _get_thread_runtime_binding(
    orchestration_service: Any, thread_target_id: str
) -> Any:
    getter = getattr(orchestration_service, "get_thread_runtime_binding", None)
    if not callable(getter) or not thread_target_id:
        return None
    try:
        return getter(thread_target_id)
    except (AttributeError, KeyError, TypeError, RuntimeError):
        return None


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
    ingress = build_surface_orchestration_ingress(
        event_sink=lambda orchestration_event: log_event_fn(
            service._logger,
            logging.INFO,
            f"discord.{orchestration_event.event_type}",
            channel_id=channel_id,
            conversation_id=context.conversation_id,
            surface_kind=orchestration_event.surface_kind,
            target_kind=orchestration_event.target_kind,
            target_id=orchestration_event.target_id,
            status=orchestration_event.status,
            **orchestration_event.metadata,
        )
    )
    paused_records: dict[str, Any] = {}

    async def _resolve_paused_flow(
        _request: SurfaceThreadMessageRequest,
    ) -> Optional[PausedFlowTarget]:
        paused = await service._find_paused_flow_run(workspace_root)
        if paused is None:
            return None
        if service._is_user_ticket_pause(workspace_root, paused):
            log_event_fn(
                service._logger,
                logging.INFO,
                "discord.flow.reply.skipped_for_user_ticket_pause",
                channel_id=channel_id,
                run_id=paused.id,
            )
            return None
        paused_records[paused.id] = paused
        paused_status = getattr(paused, "status", None)
        return PausedFlowTarget(
            flow_target=FlowTarget(
                flow_target_id="ticket_flow",
                flow_type="ticket_flow",
                display_name="ticket_flow",
                workspace_root=str(workspace_root),
            ),
            run_id=paused.id,
            status=(
                str(getattr(paused_status, "value", paused_status))
                if paused_status is not None
                else None
            ),
            workspace_root=workspace_root,
        )

    async def _submit_flow_reply(
        _request: SurfaceThreadMessageRequest, flow_target: PausedFlowTarget
    ) -> None:
        paused_record = paused_records.get(flow_target.run_id)
        if paused_record is None:
            return
        reply_text = flow_reply_text
        if has_attachments:
            (
                reply_text,
                saved_attachments,
                failed_attachments,
                transcript_message,
                _native_input_items,
            ) = await service._with_attachment_context(
                prompt_text=flow_reply_text,
                workspace_root=workspace_root,
                attachments=event.attachments,
                channel_id=channel_id,
            )
            if transcript_message:
                await service._send_channel_message_safe(
                    channel_id,
                    {
                        "content": transcript_message,
                        "allowed_mentions": {"parse": []},
                    },
                )
            if failed_attachments > 0:
                warning = (
                    "Some Discord attachments could not be downloaded. "
                    "Continuing with available inputs."
                )
                await service._send_channel_message_safe(
                    channel_id,
                    {"content": warning},
                )
            if not reply_text.strip() and saved_attachments == 0:
                await service._send_channel_message_safe(
                    channel_id,
                    {
                        "content": (
                            "Failed to download attachments from Discord. Please retry."
                        ),
                    },
                )
                return

        reply_path = service._write_user_reply(
            workspace_root, paused_record, reply_text
        )
        run_mirror = service._flow_run_mirror(workspace_root)
        run_mirror.mirror_inbound(
            run_id=flow_target.run_id,
            platform="discord",
            event_type="flow_reply_message",
            kind="command",
            actor="user",
            text=reply_text,
            chat_id=channel_id,
            thread_id=event.thread.thread_id,
            message_id=event.message.message_id,
        )
        controller = build_ticket_flow_controller_fn(workspace_root)
        try:
            updated = await controller.resume_flow(flow_target.run_id)
        except ValueError as exc:
            await service._send_channel_message_safe(
                channel_id,
                {"content": f"Failed to resume paused run: {exc}"},
            )
            return
        ensure_result = ensure_worker_fn(
            workspace_root,
            updated.id,
            is_terminal=updated.status.is_terminal(),
        )
        service._close_worker_handles(ensure_result)
        content = format_discord_message(
            f"Reply saved to `{reply_path.name}` and resumed paused run `{updated.id}`."
        )
        await service._send_channel_message_safe(channel_id, {"content": content})
        run_mirror.mirror_outbound(
            run_id=updated.id,
            platform="discord",
            event_type="flow_reply_notice",
            kind="notice",
            actor="car",
            text=content,
            chat_id=channel_id,
            thread_id=event.thread.thread_id,
        )

    async def _submit_thread_message(
        request: SurfaceThreadMessageRequest,
    ) -> DiscordMessageTurnResult:
        request_workspace_root = request.workspace_root
        prompt_text = turn_text
        (
            prompt_text,
            saved_attachments,
            failed_attachments,
            transcript_message,
            attachment_input_items,
        ) = await service._with_attachment_context(
            prompt_text=prompt_text,
            workspace_root=request_workspace_root,
            attachments=event.attachments,
            channel_id=channel_id,
        )
        if transcript_message:
            await service._send_channel_message_safe(
                channel_id,
                {
                    "content": transcript_message,
                    "allowed_mentions": {"parse": []},
                },
            )
        if failed_attachments > 0:
            warning = (
                "Some Discord attachments could not be downloaded. "
                "Continuing with available inputs."
            )
            await service._send_channel_message_safe(
                channel_id,
                {"content": warning},
            )
        if not prompt_text.strip():
            if has_attachments and saved_attachments == 0:
                await service._send_channel_message_safe(
                    channel_id,
                    {
                        "content": (
                            "Failed to download attachments from Discord. Please retry."
                        ),
                    },
                )
            return DiscordMessageTurnResult(
                final_message="",
                send_final_message=False,
            )

        if not effective_pma_enabled:
            prompt_text, injected = maybe_inject_car_awareness(
                prompt_text,
                declared_profile="car_ambient",
            )
            if injected:
                log_event_fn(
                    service._logger,
                    logging.INFO,
                    "discord.car_context.injected",
                    channel_id=channel_id,
                    message_id=event.message.message_id,
                )
            prompt_text, injected = maybe_inject_prompt_writing_hint(
                prompt_text,
                trigger_text=text,
            )
            if injected:
                log_event_fn(
                    service._logger,
                    logging.INFO,
                    "discord.prompt_context.injected",
                    channel_id=channel_id,
                    message_id=event.message.message_id,
                )
            prompt_text, injected = _maybe_inject_discord_filebox_hint(
                prompt_text,
                user_text=text,
                workspace_root=request_workspace_root,
            )
            if injected:
                log_event_fn(
                    service._logger,
                    logging.INFO,
                    "discord.filebox_context.injected",
                    channel_id=channel_id,
                    message_id=event.message.message_id,
                )

        if effective_pma_enabled:
            try:
                snapshot = await build_hub_snapshot(
                    service._hub_supervisor, hub_root=service._config.root
                )
                prompt_base = load_pma_prompt(service._config.root)
                if notification_reply is not None:
                    prompt_text = (
                        f"{build_notification_context_block(notification_reply)}\n\n"
                        f"{prompt_text}"
                    )
                prompt_text = format_pma_prompt(
                    prompt_base,
                    snapshot,
                    prompt_text,
                    hub_root=service._config.root,
                    prompt_state_key=session_key,
                )
            except (OSError, ValueError, KeyError, TypeError) as exc:
                log_event_fn(
                    service._logger,
                    logging.WARNING,
                    "discord.pma.prompt_build.failed",
                    channel_id=channel_id,
                    exc=exc,
                )
                await service._send_channel_message_safe(
                    channel_id,
                    {"content": "Failed to build PMA context. Please try again."},
                )
                return DiscordMessageTurnResult(
                    final_message="",
                    send_final_message=False,
                )

        prompt_text, _github_injected = await service._maybe_inject_github_context(
            prompt_text,
            request_workspace_root,
            link_source_text=turn_text,
            allow_cross_repo=pma_enabled,
        )
        if pending_compact_seed:
            prompt_text = f"{pending_compact_seed}\n\n{prompt_text}"

        turn_input_items: Optional[list[dict[str, Any]]] = None
        if attachment_input_items:
            turn_input_items = [
                {"type": "text", "text": prompt_text},
                *attachment_input_items,
            ]
        run_turn_kwargs: dict[str, Any] = {
            "workspace_root": request_workspace_root,
            "prompt_text": prompt_text,
            "agent": agent,
            "model_override": model_override,
            "reasoning_effort": reasoning_effort,
            "session_key": session_key,
            "orchestrator_channel_key": (
                channel_id if not effective_pma_enabled else f"pma:{channel_id}"
            ),
        }
        if notification_reply is not None:
            surface_key = notification_surface_key(notification_reply.notification_id)
            try:
                if (
                    "managed_thread_surface_key"
                    in inspect.signature(service._run_agent_turn_for_message).parameters
                ):
                    run_turn_kwargs["managed_thread_surface_key"] = surface_key
            except (TypeError, ValueError):
                pass
        try:
            if (
                "source_message_id"
                in inspect.signature(service._run_agent_turn_for_message).parameters
            ):
                run_turn_kwargs["source_message_id"] = event.message.message_id
        except (TypeError, ValueError):
            pass
        if turn_input_items:
            run_turn_kwargs["input_items"] = turn_input_items
        try:
            return cast(
                DiscordMessageTurnResult,
                await service._run_agent_turn_for_message(**run_turn_kwargs),
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
            log_event_fn(
                service._logger,
                logging.WARNING,
                "discord.turn.failed",
                channel_id=channel_id,
                conversation_id=context.conversation_id,
                workspace_root=str(workspace_root),
                agent=agent,
                exc=exc,
            )
            await service._send_channel_message_safe(
                channel_id,
                {
                    "content": (
                        f"Turn failed: {exc} (conversation {context.conversation_id})"
                    )
                },
            )
            return DiscordMessageTurnResult(
                final_message="",
                send_final_message=False,
            )

    if notification_reply is not None:
        notification_workspace_root = workspace_root
        stored_workspace_root = getattr(notification_reply, "workspace_root", None)
        if isinstance(stored_workspace_root, str) and stored_workspace_root.strip():
            notification_workspace_root = Path(stored_workspace_root)
        turn_result = await _submit_thread_message(
            SurfaceThreadMessageRequest(
                surface_kind="discord",
                workspace_root=notification_workspace_root,
                prompt_text=turn_text,
                agent_id=runtime_agent,
                pma_enabled=True,
            )
        )
        surface_key = notification_surface_key(notification_reply.notification_id)
        orch_binding = build_discord_thread_orchestration_service(service).get_binding(
            surface_kind="discord",
            surface_key=surface_key,
        )
        if orch_binding is not None:
            PmaNotificationStore(service._config.root).bind_continuation_thread(
                notification_id=notification_reply.notification_id,
                thread_target_id=orch_binding.thread_target_id,
            )
    else:
        result = await ingress.submit_message(
            SurfaceThreadMessageRequest(
                surface_kind="discord",
                workspace_root=workspace_root,
                prompt_text=turn_text,
                agent_id=runtime_agent,
                pma_enabled=pma_enabled,
            ),
            resolve_paused_flow_target=_resolve_paused_flow,
            submit_flow_reply=_submit_flow_reply,
            submit_thread_message=_submit_thread_message,
        )
        if result.route == "flow":
            return
        turn_result = result.thread_result

    if isinstance(turn_result, DiscordMessageTurnResult):
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
            agent=agent,
            model=model_override,
        )
    else:
        response_text = str(turn_result or "")
        preview_message_id = None
        send_final_message = True
        intermediate_text = ""

    if isinstance(preview_message_id, str) and preview_message_id:
        await service._delete_channel_message_safe(
            channel_id=channel_id,
            message_id=preview_message_id,
            record_id=f"turn:delete_progress:{session_key}:{uuid.uuid4().hex[:8]}",
        )
    if send_final_message:
        await _send_discord_turn_section(
            service,
            channel_id=channel_id,
            text=response_text or "(No response text returned.)",
            record_prefix=f"turn:final:{session_key}",
            attachment_filename="final-response.md",
            attachment_caption="Final response too long; attached as final-response.md.",
        )
    if pending_compact_seed is not None:
        await service._store.clear_pending_compact_seed(channel_id=channel_id)
    if send_final_message:
        await service._flush_outbox_files(
            workspace_root=workspace_root,
            channel_id=channel_id,
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


def _note_runtime_event_state(
    event_state: RuntimeThreadRunEventState,
    run_event: Any,
) -> None:
    if isinstance(run_event, OutputDelta):
        if run_event.delta_type == RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM:
            event_state.note_stream_text(str(run_event.content or ""))
            return
        if run_event.delta_type == RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE:
            event_state.note_message_text(str(run_event.content or ""))
            return
        return
    if isinstance(run_event, TokenUsage) and isinstance(run_event.usage, dict):
        event_state.token_usage = dict(run_event.usage)
        return
    if isinstance(run_event, Completed):
        event_state.completed_seen = True
        if isinstance(run_event.final_message, str):
            event_state.note_message_text(run_event.final_message)
        return
    if isinstance(run_event, Failed):
        error_message = str(run_event.error_message or "").strip()
        if error_message:
            event_state.last_error_message = error_message


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


def build_discord_thread_orchestration_service(service: Any) -> Any:
    cached = getattr(service, "_discord_thread_orchestration_service", None)
    if cached is None:
        cached = getattr(service, "_discord_managed_thread_orchestration_service", None)
    if cached is not None:
        return cached

    try:
        descriptors = get_registered_agents(service)
    except TypeError as exc:
        if "positional argument" not in str(exc):
            raise
        descriptors = get_registered_agents()

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
    binding = orchestration_service.get_binding(
        surface_kind="discord",
        surface_key=surface_key,
    )
    thread_target_id = (
        binding.thread_target_id
        if binding is not None and str(binding.mode or "").strip().lower() == mode
        else None
    )
    thread: Any = (
        orchestration_service.get_thread_target(thread_target_id)
        if isinstance(thread_target_id, str) and thread_target_id
        else None
    )
    canonical_workspace = str(workspace_root.resolve())
    reusable_thread = (
        thread is not None
        and thread.agent_id == agent
        and (thread.agent_profile or None) == (agent_profile or None)
        and str(thread.workspace_root or "").strip() == canonical_workspace
    )
    owner_kind, owner_id, normalized_repo_id = service._resource_owner_for_workspace(
        workspace_root,
        repo_id=repo_id,
        resource_kind=resource_kind,
        resource_id=resource_id,
    )
    if (
        reusable_thread
        and str(thread.lifecycle_status or "").strip().lower() != "active"
    ):
        thread = orchestration_service.resume_thread_target(thread.thread_target_id)
    elif not reusable_thread:
        thread_metadata: Optional[dict[str, Any]] = (
            {"agent_profile": agent_profile} if agent_profile else None
        )
        thread = orchestration_service.create_thread_target(
            agent,
            workspace_root,
            repo_id=normalized_repo_id,
            resource_kind=owner_kind,
            resource_id=owner_id,
            display_name=f"discord:{surface_key}",
            metadata=thread_metadata,
        )
    orchestration_service.upsert_binding(
        surface_kind="discord",
        surface_key=surface_key,
        thread_target_id=thread.thread_target_id,
        agent_id=agent,
        repo_id=normalized_repo_id,
        resource_kind=owner_kind,
        resource_id=owner_id,
        mode=mode,
        metadata={"channel_id": channel_id, "pma_enabled": pma_enabled},
    )
    return orchestration_service, thread


async def _finalize_discord_thread_execution(
    service: Any,
    *,
    orchestration_service: Any,
    started: RuntimeThreadExecution,
    channel_id: str,
    public_execution_error: str,
    timeout_error: str,
    interrupted_error: str,
    runtime_event_state: Optional[RuntimeThreadRunEventState] = None,
    on_progress_event: Optional[Any] = None,
) -> dict[str, Any]:
    thread_store = PmaThreadStore(service._config.root)
    transcripts = PmaTranscriptStore(service._config.root)
    managed_thread_id = started.thread.thread_target_id
    managed_turn_id = started.execution.execution_id
    current_thread_row = thread_store.get_thread(managed_thread_id) or {}
    current_preview = truncate_for_discord(
        str(started.request.message_text or ""),
        max_len=120,
    )
    runtime_binding = _get_thread_runtime_binding(
        orchestration_service, managed_thread_id
    )
    current_backend_thread_id = str(
        getattr(runtime_binding, "backend_thread_id", None)
        or started.thread.backend_thread_id
        or ""
    ).strip()
    started_execution_status = str(
        getattr(started.execution, "status", "") or ""
    ).strip()
    started_execution_error = str(getattr(started.execution, "error", "") or "").strip()
    event_state = runtime_event_state or RuntimeThreadRunEventState()
    stream_task: Optional[asyncio.Task[None]] = None
    timeline_events: list[Any] = []
    live_timeline_count = 0
    live_timeline_error_logged = False

    def _persist_live_timeline_events(events: list[Any]) -> None:
        nonlocal live_timeline_count
        nonlocal live_timeline_error_logged
        if not events:
            return
        try:
            persist_turn_timeline(
                service._config.root,
                execution_id=managed_turn_id,
                target_kind="thread_target",
                target_id=managed_thread_id,
                repo_id=str(current_thread_row.get("repo_id") or "").strip() or None,
                resource_kind=(
                    str(current_thread_row.get("resource_kind") or "").strip() or None
                ),
                resource_id=(
                    str(current_thread_row.get("resource_id") or "").strip() or None
                ),
                metadata={
                    "agent": getattr(started.thread, "agent_id", None),
                    "execution_id": managed_turn_id,
                    "thread_target_id": managed_thread_id,
                    "backend_thread_id": current_backend_thread_id or None,
                    "backend_turn_id": started.execution.backend_id,
                    "model": started.request.model,
                    "reasoning": started.request.reasoning,
                    "request_kind": getattr(started.request, "kind", None),
                    "status": "running",
                    "surface_kind": "discord",
                    "surface_key": channel_id,
                },
                events=events,
                start_index=live_timeline_count + 1,
            )
        except Exception:
            if not live_timeline_error_logged:
                live_timeline_error_logged = True
                _logger.exception(
                    "Failed to persist live Discord thread timeline (thread=%s turn=%s)",
                    managed_thread_id,
                    managed_turn_id,
                )
        else:
            live_timeline_count += len(events)

    stream_backend_thread_id = current_backend_thread_id
    stream_backend_turn_id = str(started.execution.backend_id or "").strip()
    if not stream_backend_turn_id:
        stream_backend_turn_id = str(started.execution.execution_id or "").strip()
        _logger.warning(
            "Discord finalize: backend_id missing, falling back to execution_id=%s "
            "for thread=%s",
            stream_backend_turn_id,
            managed_thread_id,
        )

    if (
        harness_supports_progress_event_stream(started.harness)
        and stream_backend_thread_id
        and stream_backend_turn_id
    ):

        async def _pump_runtime_events() -> None:
            raw_events_received = 0
            run_events_dispatched = 0
            try:
                async for raw_event in harness_progress_event_stream(
                    started.harness,
                    started.workspace_root,
                    stream_backend_thread_id,
                    stream_backend_turn_id,
                ):
                    raw_events_received += 1
                    run_events: list[Any]
                    if isinstance(
                        raw_event,
                        (
                            OutputDelta,
                            ToolCall,
                            ApprovalRequested,
                            RunNotice,
                            TokenUsage,
                            Completed,
                            Failed,
                            Started,
                        ),
                    ):
                        run_events = [raw_event]
                        for run_event in run_events:
                            _note_runtime_event_state(event_state, run_event)
                    else:
                        run_events = await normalize_runtime_thread_raw_event(
                            raw_event,
                            event_state,
                        )
                    timeline_events.extend(run_events)
                    _persist_live_timeline_events(run_events)
                    if on_progress_event is None:
                        continue
                    for run_event in run_events:
                        run_events_dispatched += 1
                        try:
                            await on_progress_event(run_event)
                        except (
                            RuntimeError,
                            ConnectionError,
                            OSError,
                            ValueError,
                            TypeError,
                        ):
                            _logger.debug(
                                "Discord progress event handler failed for %s",
                                type(run_event).__name__,
                                exc_info=True,
                            )
                            continue
            except (RuntimeError, ConnectionError, OSError, ValueError, TypeError):
                _logger.warning("Discord progress event pump failed", exc_info=True)
            finally:
                _logger.info(
                    "Discord progress pump finished thread=%s turn=%s "
                    "raw_events=%d run_events_dispatched=%d",
                    managed_thread_id,
                    managed_turn_id,
                    raw_events_received,
                    run_events_dispatched,
                )

        stream_task = asyncio.create_task(_pump_runtime_events())

    try:
        if started_execution_status == "error":
            outcome = RuntimeThreadOutcome(
                status="error",
                assistant_text="",
                error=started_execution_error or public_execution_error,
                backend_thread_id=current_backend_thread_id,
                backend_turn_id=started.execution.backend_id,
            )
        elif started_execution_status == "interrupted":
            outcome = RuntimeThreadOutcome(
                status="interrupted",
                assistant_text="",
                error=interrupted_error,
                backend_thread_id=current_backend_thread_id,
                backend_turn_id=started.execution.backend_id,
            )
        else:
            outcome = await await_runtime_thread_outcome(
                started,
                interrupt_event=None,
                timeout_seconds=DISCORD_PMA_TIMEOUT_SECONDS,
                execution_error_message=public_execution_error,
            )
    except (RuntimeError, ConnectionError, OSError, ValueError, TypeError):
        outcome = RuntimeThreadOutcome(
            status="error",
            assistant_text="",
            error=started_execution_error or public_execution_error,
            backend_thread_id=current_backend_thread_id,
            backend_turn_id=started.execution.backend_id,
        )
    finally:
        if stream_task is not None:
            drain_cancel: Optional[BaseException] = None
            try:
                await asyncio.wait_for(stream_task, timeout=0.5)
            except asyncio.TimeoutError:
                stream_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stream_task
            except asyncio.CancelledError as exc:
                drain_cancel = exc
                stream_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stream_task
            if drain_cancel is not None:
                raise drain_cancel

    recovered_outcome = recover_post_completion_outcome(outcome, event_state)
    if recovered_outcome is not outcome:
        service._logger.warning(
            "Discord runtime turn recovered from post-completion error: thread=%s turn=%s error=%s",
            managed_thread_id,
            managed_turn_id,
            outcome.error,
        )
        outcome = recovered_outcome

    if on_progress_event is not None:
        terminal_event = terminal_run_event_from_outcome(outcome, event_state)
        timeline_events.append(terminal_event)
        try:
            await on_progress_event(terminal_event)
        except (RuntimeError, ConnectionError, OSError):
            _logger.debug("Discord terminal progress event failed", exc_info=True)
    else:
        timeline_events.append(terminal_run_event_from_outcome(outcome, event_state))

    try:
        persist_turn_timeline(
            service._config.root,
            execution_id=managed_turn_id,
            target_kind="thread_target",
            target_id=managed_thread_id,
            repo_id=str(current_thread_row.get("repo_id") or "").strip() or None,
            resource_kind=(
                str(current_thread_row.get("resource_kind") or "").strip() or None
            ),
            resource_id=(
                str(current_thread_row.get("resource_id") or "").strip() or None
            ),
            metadata={
                "agent": getattr(started.thread, "agent_id", None),
                "execution_id": managed_turn_id,
                "thread_target_id": managed_thread_id,
                "backend_thread_id": current_backend_thread_id or None,
                "backend_turn_id": outcome.backend_turn_id
                or started.execution.backend_id,
                "model": started.request.model,
                "reasoning": started.request.reasoning,
                "request_kind": getattr(started.request, "kind", None),
                "status": outcome.status,
                "surface_kind": "discord",
                "surface_key": channel_id,
            },
            events=timeline_events,
        )
    except Exception:
        _logger.exception(
            "Failed to persist Discord thread timeline (thread=%s turn=%s)",
            managed_thread_id,
            managed_turn_id,
        )

    resolved_assistant_text = (
        outcome.assistant_text or event_state.best_assistant_text()
    )

    finalized_thread = orchestration_service.get_thread_target(managed_thread_id)
    finalized_runtime_binding = _get_thread_runtime_binding(
        orchestration_service, managed_thread_id
    )
    resolved_backend_thread_id = (
        str(
            getattr(finalized_runtime_binding, "backend_thread_id", None)
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
            "surface_kind": "discord",
            "surface_key": channel_id,
        }
        try:
            transcripts.write_transcript(
                turn_id=managed_turn_id,
                metadata=transcript_metadata,
                assistant_text=resolved_assistant_text,
            )
            transcript_turn_id = managed_turn_id
        except OSError as exc:
            service._logger.warning(
                "Failed to persist Discord transcript (thread=%s turn=%s): %s",
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
                error=outcome.error,
                backend_turn_id=outcome.backend_turn_id,
                transcript_turn_id=transcript_turn_id,
            )
        except KeyError:
            finalized_execution = orchestration_service.get_execution(
                managed_thread_id, managed_turn_id
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
                managed_thread_id, managed_turn_id
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


def _ensure_discord_thread_queue_worker(
    service: Any,
    *,
    orchestration_service: Any,
    managed_thread_id: str,
    channel_id: str,
    public_execution_error: str,
    timeout_error: str,
    interrupted_error: str,
) -> None:
    task_map = getattr(service, "_discord_thread_queue_tasks", None)
    if not isinstance(task_map, dict):
        task_map = {}
        service._discord_thread_queue_tasks = task_map
        service._discord_managed_thread_queue_tasks = task_map
    existing = task_map.get(managed_thread_id)
    if isinstance(existing, asyncio.Task) and not existing.done():
        return

    worker_task: Optional[asyncio.Task[Any]] = None

    async def _run_with_discord_typing_indicator(
        *,
        channel_id: str,
        work: Any,
    ) -> None:
        run_with_typing = getattr(service, "_run_with_typing_indicator", None)
        if callable(run_with_typing):
            await run_with_typing(channel_id=channel_id, work=work)
            return
        await work()

    async def _queue_worker() -> None:
        try:
            while True:
                if (
                    orchestration_service.get_running_execution(managed_thread_id)
                    is not None
                ):
                    await asyncio.sleep(0.1)
                    continue
                started = await begin_next_queued_runtime_thread_execution(
                    orchestration_service,
                    managed_thread_id,
                )
                if started is None:
                    break

                async def _process_started_execution(
                    started_execution: RuntimeThreadExecution,
                ) -> None:
                    service._register_discord_turn_approval_context(
                        started_execution=started_execution,
                        channel_id=channel_id,
                    )
                    try:
                        finalized = await _finalize_discord_thread_execution(
                            service,
                            orchestration_service=orchestration_service,
                            started=started_execution,
                            channel_id=channel_id,
                            public_execution_error=public_execution_error,
                            timeout_error=timeout_error,
                            interrupted_error=interrupted_error,
                        )
                    finally:
                        service._clear_discord_turn_approval_context(
                            started_execution=started_execution
                        )
                    if finalized["status"] == "ok":
                        assistant_text = str(
                            finalized.get("assistant_text") or ""
                        ).strip()
                        message = (
                            format_discord_message(assistant_text)
                            if assistant_text
                            else "(No response text returned.)"
                        )
                        await service._send_channel_message_safe(
                            channel_id,
                            {"content": message},
                            record_id=(
                                "discord-queued:"
                                f"{managed_thread_id}:{finalized['managed_turn_id']}"
                            ),
                        )
                        return
                    await service._send_channel_message_safe(
                        channel_id,
                        {
                            "content": (
                                f"Turn failed: {finalized.get('error') or public_execution_error}"
                            )
                        },
                        record_id=(
                            "discord-queued-error:"
                            f"{managed_thread_id}:{finalized['managed_turn_id']}"
                        ),
                    )

                await _run_with_discord_typing_indicator(
                    channel_id=channel_id,
                    work=functools.partial(_process_started_execution, started),
                )
        finally:
            if (
                worker_task is not None
                and task_map.get(managed_thread_id) is worker_task
            ):
                task_map.pop(managed_thread_id, None)

    worker_task = service._spawn_task(_queue_worker())
    task_map[managed_thread_id] = worker_task


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
    runtime_agent = service._runtime_agent_for_binding(binding)
    _agent, agent_profile = service._resolve_agent_state(binding)
    repo_id = binding.get("repo_id") if isinstance(binding, dict) else None
    resource_kind = binding.get("resource_kind") if isinstance(binding, dict) else None
    resource_id = binding.get("resource_id") if isinstance(binding, dict) else None
    orchestration_service, thread = resolve_discord_thread_target(
        service,
        channel_id=channel_id,
        managed_thread_surface_key=managed_thread_surface_key,
        workspace_root=workspace_root,
        agent=runtime_agent,
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
            payload["components"] = [build_cancel_turn_button()]
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
                    "components": [build_cancel_turn_button()],
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
        started_execution = await begin_runtime_thread_execution(
            orchestration_service,
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
        )
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

    if (
        str(getattr(started_execution.execution, "status", "") or "").strip()
        == "queued"
    ):
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
        _ensure_discord_thread_queue_worker(
            service,
            orchestration_service=orchestration_service,
            managed_thread_id=managed_thread_id,
            channel_id=channel_id,
            public_execution_error=public_execution_error,
            timeout_error=timeout_error,
            interrupted_error=interrupted_error,
        )
        return DiscordMessageTurnResult(
            final_message="Queued (waiting for available worker...)"
        )

    service._register_discord_turn_approval_context(
        started_execution=started_execution,
        channel_id=channel_id,
    )
    try:
        finalized = await _finalize_discord_thread_execution(
            service,
            orchestration_service=orchestration_service,
            started=started_execution,
            channel_id=channel_id,
            public_execution_error=public_execution_error,
            timeout_error=timeout_error,
            interrupted_error=interrupted_error,
            runtime_event_state=RuntimeThreadRunEventState(),
            on_progress_event=lambda run_event: _apply_discord_progress_run_event(
                tracker,
                run_event,
                runtime_state=runtime_state,
                edit_progress=_edit_progress,
            ),
        )
    finally:
        service._clear_discord_turn_approval_context(
            started_execution=started_execution
        )
        await _stop_progress_heartbeat()

    _ensure_discord_thread_queue_worker(
        service,
        orchestration_service=orchestration_service,
        managed_thread_id=managed_thread_id,
        channel_id=channel_id,
        public_execution_error=public_execution_error,
        timeout_error=timeout_error,
        interrupted_error=interrupted_error,
    )
    if finalized["status"] != "ok":
        if finalized["status"] == "interrupted":
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
        raise RuntimeError(str(finalized.get("error") or public_execution_error))
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
        final_message=str(finalized.get("assistant_text") or ""),
        preview_message_id=progress_message_id,
        intermediate_message=intermediate_message,
        token_usage=cast(Optional[dict[str, Any]], finalized.get("token_usage")),
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
