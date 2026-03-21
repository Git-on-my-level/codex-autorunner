from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Optional, cast

from ...agents.base import (
    harness_progress_event_stream,
    harness_supports_progress_event_stream,
)
from ...agents.registry import get_registered_agents
from ...core.context_awareness import (
    maybe_inject_car_awareness,
    maybe_inject_prompt_writing_hint,
)
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
from ...core.pma_context import (
    build_hub_snapshot,
    format_pma_discoverability_preamble,
    format_pma_prompt,
    load_pma_prompt,
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
from ...integrations.chat.forwarding import compose_forwarded_message_text
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
    truncate_for_discord,
)

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


_sanitize_runtime_thread_result_error = sanitize_runtime_thread_error


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
    turn_text = compose_forwarded_message_text(text, event.forwarded_from)
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

    agent = service._normalize_agent(binding.get("agent"))
    model_override = binding.get("model_override")
    if not isinstance(model_override, str) or not model_override.strip():
        model_override = None
    reasoning_effort = binding.get("reasoning_effort")
    if not isinstance(reasoning_effort, str) or not reasoning_effort.strip():
        reasoning_effort = None
    session_key = service._build_message_session_key(
        channel_id=channel_id,
        workspace_root=workspace_root,
        pma_enabled=pma_enabled,
        agent=agent,
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
        reply_text = turn_text
        if has_attachments:
            (
                reply_text,
                saved_attachments,
                failed_attachments,
                transcript_message,
                _native_input_items,
            ) = await service._with_attachment_context(
                prompt_text=turn_text,
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
        _request: SurfaceThreadMessageRequest,
    ) -> DiscordMessageTurnResult:
        prompt_text = turn_text
        (
            prompt_text,
            saved_attachments,
            failed_attachments,
            transcript_message,
            attachment_input_items,
        ) = await service._with_attachment_context(
            prompt_text=prompt_text,
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

        if not pma_enabled:
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
            prompt_text, injected = maybe_inject_prompt_writing_hint(prompt_text)
            if injected:
                log_event_fn(
                    service._logger,
                    logging.INFO,
                    "discord.prompt_context.injected",
                    channel_id=channel_id,
                    message_id=event.message.message_id,
                )

        if pma_enabled:
            try:
                snapshot = await build_hub_snapshot(
                    service._hub_supervisor, hub_root=service._config.root
                )
                prompt_base = load_pma_prompt(service._config.root)
                prompt_text = format_pma_prompt(
                    prompt_base,
                    snapshot,
                    prompt_text,
                    hub_root=service._config.root,
                    prompt_state_key=session_key,
                )
            except Exception as exc:
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
            workspace_root,
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
            "workspace_root": workspace_root,
            "prompt_text": prompt_text,
            "agent": agent,
            "model_override": model_override,
            "reasoning_effort": reasoning_effort,
            "session_key": session_key,
            "orchestrator_channel_key": (
                channel_id if not pma_enabled else f"pma:{channel_id}"
            ),
        }
        if turn_input_items:
            run_turn_kwargs["input_items"] = turn_input_items
        try:
            return cast(
                DiscordMessageTurnResult,
                await service._run_agent_turn_for_message(**run_turn_kwargs),
            )
        except Exception as exc:
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

    result = await ingress.submit_message(
        SurfaceThreadMessageRequest(
            surface_kind="discord",
            workspace_root=workspace_root,
            prompt_text=turn_text,
            agent_id=agent,
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


def _should_use_legacy_orchestrator_harness(service: Any) -> bool:
    bound = getattr(service, "_orchestrator_for_workspace", None)
    if bound is None:
        return False
    original = getattr(type(service), "_orchestrator_for_workspace", None)
    bound_func = getattr(bound, "__func__", None)
    if original is not None and bound_func is not None:
        if bound_func is not original:
            return True
    elif original is not None and bound is not original:
        return True
    return bool(getattr(service, "_backend_orchestrator_factory", None))


class _LegacyOrchestratorRuntimeHarness:
    display_name = "Legacy Discord Orchestrator"
    capabilities = frozenset(
        {"durable_threads", "message_turns", "interrupt", "event_streaming"}
    )

    def __init__(self, service: Any, agent_id: str) -> None:
        self._service = service
        self._agent_id = agent_id
        self._turns: dict[str, dict[str, Any]] = {}

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    async def ensure_ready(self, workspace_root: Path) -> None:
        _ = workspace_root

    async def _orchestrator(self, workspace_root: Path) -> Any:
        return await self._service._orchestrator_for_workspace(
            workspace_root,
            channel_id=f"discord-orchestration:{self._agent_id}",
        )

    async def new_conversation(
        self, workspace_root: Path, title: Optional[str] = None
    ) -> SimpleNamespace:
        _ = workspace_root, title
        return SimpleNamespace(id=f"legacy-thread-{uuid.uuid4().hex[:8]}")

    async def resume_conversation(
        self, workspace_root: Path, conversation_id: str
    ) -> SimpleNamespace:
        _ = workspace_root
        return SimpleNamespace(id=conversation_id)

    async def start_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        prompt: str,
        model: Optional[str],
        reasoning: Optional[str],
        *,
        approval_mode: Optional[str],
        sandbox_policy: Optional[Any],
        input_items: Optional[list[dict[str, Any]]] = None,
    ) -> SimpleNamespace:
        _ = approval_mode, sandbox_policy
        orchestrator = await self._orchestrator(workspace_root)
        state = self._service._build_runner_state(
            agent=self._agent_id,
            model_override=model,
            reasoning_effort=reasoning,
        )
        run_events = orchestrator.run_turn(
            agent_id=self._agent_id,
            state=state,
            prompt=prompt,
            input_items=input_items,
            model=model,
            reasoning=reasoning,
            session_key=conversation_id,
            session_id=conversation_id,
            workspace_root=workspace_root,
        )
        queue: asyncio.Queue[Any] = asyncio.Queue()
        done = asyncio.Event()
        result: dict[str, Any] = {
            "status": "running",
            "assistant_text": "",
            "error": None,
        }
        session_id = conversation_id
        assistant_fallback = ""
        final_message = ""
        completed_seen = False
        turn_id = f"legacy-turn-{uuid.uuid4().hex[:8]}"

        def _merge_assistant_stream(current: str, incoming: str) -> str:
            if not incoming:
                return current
            if not current:
                return incoming
            if len(incoming) > len(current) and incoming.startswith(current):
                return incoming
            max_overlap = min(len(current), max(len(incoming) - 1, 0))
            for overlap in range(max_overlap, 0, -1):
                if current[-overlap:] == incoming[:overlap]:
                    return f"{current}{incoming[overlap:]}"
            return f"{current}{incoming}"

        def _record_event(event: Any) -> None:
            nonlocal session_id
            nonlocal assistant_fallback
            nonlocal final_message
            nonlocal completed_seen
            if isinstance(event, Started):
                if isinstance(event.session_id, str) and event.session_id:
                    session_id = event.session_id
            elif isinstance(event, OutputDelta):
                if (
                    event.delta_type
                    in {
                        RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
                        RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
                    }
                    and isinstance(event.content, str)
                    and event.content
                ):
                    assistant_fallback = _merge_assistant_stream(
                        assistant_fallback,
                        event.content,
                    )
            elif isinstance(event, Completed):
                final_message = event.final_message or final_message
                completed_seen = True
            elif isinstance(event, Failed) and not completed_seen:
                result["error"] = event.error_message or "Turn failed"
            elif isinstance(event, Failed):
                from . import service as discord_service_module

                discord_service_module.log_event(
                    self._service._logger,
                    logging.WARNING,
                    "discord.turn.failed_late_ignored",
                    conversation_id=session_id,
                    agent=self._agent_id,
                    error_message=event.error_message or "Turn failed",
                )

        try:
            first_event = await run_events.__anext__()
        except StopAsyncIteration:
            first_event = None
        if first_event is not None:
            _record_event(first_event)
            queue.put_nowait(first_event)

        async def _pump_remaining() -> None:
            try:
                async for event in run_events:
                    _record_event(event)
                    await queue.put(event)
            except Exception as exc:
                result["error"] = str(exc) or "Turn failed"
            finally:
                if completed_seen or not result.get("error"):
                    result["status"] = "ok"
                    result["error"] = None
                    result["assistant_text"] = final_message or assistant_fallback
                else:
                    result["status"] = "error"
                    result["assistant_text"] = ""
                done.set()

        task = asyncio.create_task(_pump_remaining())
        self._turns[turn_id] = {
            "queue": queue,
            "done": done,
            "task": task,
            "result": result,
            "state": state,
        }
        return SimpleNamespace(conversation_id=session_id, turn_id=turn_id)

    async def start_review(self, *args: Any, **kwargs: Any) -> SimpleNamespace:
        raise AssertionError("legacy harness review mode is not implemented")

    async def wait_for_turn(
        self,
        workspace_root: Path,
        conversation_id: str,
        turn_id: Optional[str],
        *,
        timeout: Optional[float] = None,
    ) -> SimpleNamespace:
        _ = workspace_root, conversation_id, timeout
        if not isinstance(turn_id, str) or turn_id not in self._turns:
            return SimpleNamespace(
                status="error",
                assistant_text="",
                errors=["Turn failed"],
            )
        turn = self._turns[turn_id]
        await turn["done"].wait()
        result = turn["result"]
        errors = [result["error"]] if result.get("error") else []
        return SimpleNamespace(
            status=result["status"],
            assistant_text=result["assistant_text"],
            errors=errors,
        )

    async def interrupt(
        self, workspace_root: Path, conversation_id: str, turn_id: Optional[str]
    ) -> None:
        turn = self._turns.get(turn_id or "")
        orchestrator = await self._orchestrator(workspace_root)
        interrupt = getattr(orchestrator, "interrupt", None)
        if callable(interrupt):
            state = turn.get("state") if isinstance(turn, dict) else None
            await interrupt(self._agent_id, state)

    async def stream_events(
        self, workspace_root: Path, conversation_id: str, turn_id: str
    ):
        _ = workspace_root, conversation_id
        turn = self._turns.get(turn_id)
        if not isinstance(turn, dict):
            if False:
                yield ""
            return
        queue = turn["queue"]
        done = turn["done"]
        while True:
            if done.is_set() and queue.empty():
                break
            event = await queue.get()
            yield event


def build_discord_thread_orchestration_service(service: Any) -> Any:
    cached = getattr(service, "_discord_thread_orchestration_service", None)
    if cached is None:
        cached = getattr(service, "_discord_managed_thread_orchestration_service", None)
    if cached is not None:
        return cached

    descriptors = get_registered_agents()

    def _make_harness(agent_id: str) -> Any:
        if _should_use_legacy_orchestrator_harness(service):
            return _LegacyOrchestratorRuntimeHarness(service, agent_id)
        descriptor = descriptors.get(agent_id)
        if descriptor is None:
            raise KeyError(f"Unknown agent definition '{agent_id}'")
        return descriptor.make_harness(service)

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
    workspace_root: Path,
    agent: str,
    repo_id: Optional[str],
    resource_kind: Optional[str],
    resource_id: Optional[str],
    mode: str,
    pma_enabled: bool,
) -> Any:
    orchestration_service = build_discord_thread_orchestration_service(service)
    binding = orchestration_service.get_binding(
        surface_kind="discord",
        surface_key=channel_id,
    )
    thread_target_id = (
        binding.thread_target_id
        if binding is not None and str(binding.mode or "").strip().lower() == mode
        else None
    )
    thread = (
        orchestration_service.get_thread_target(thread_target_id)
        if isinstance(thread_target_id, str) and thread_target_id
        else None
    )
    canonical_workspace = str(workspace_root.resolve())
    reusable_thread = (
        thread is not None
        and thread.agent_id == agent
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
        thread = orchestration_service.create_thread_target(
            agent,
            workspace_root,
            repo_id=normalized_repo_id,
            resource_kind=owner_kind,
            resource_id=owner_id,
            display_name=f"discord:{channel_id}",
        )
    orchestration_service.upsert_binding(
        surface_kind="discord",
        surface_key=channel_id,
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
    current_backend_thread_id = str(started.thread.backend_thread_id or "").strip()
    event_state = runtime_event_state or RuntimeThreadRunEventState()
    stream_task: Optional[asyncio.Task[None]] = None

    stream_backend_thread_id = str(started.thread.backend_thread_id or "").strip()
    if not stream_backend_thread_id:
        stream_backend_thread_id = str(started.thread.thread_target_id or "").strip()
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
            timeout_seconds=DISCORD_PMA_TIMEOUT_SECONDS,
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
            with contextlib.suppress(asyncio.CancelledError):
                await stream_task

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
        with contextlib.suppress(Exception):
            await on_progress_event(
                terminal_run_event_from_outcome(outcome, event_state)
            )

    resolved_assistant_text = (
        outcome.assistant_text or event_state.best_assistant_text()
    )

    finalized_thread = orchestration_service.get_thread_target(managed_thread_id)
    resolved_backend_thread_id = (
        str(getattr(finalized_thread, "backend_thread_id", None) or "").strip()
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
        except Exception as exc:
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
                error=None,
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
                    started_execution: RuntimeThreadExecution = started,
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
                        message = str(finalized.get("assistant_text") or "").strip()
                        if message:
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
                    work=_process_started_execution,
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
    agent: str,
    model_override: Optional[str],
    reasoning_effort: Optional[str],
    session_key: str,
    orchestrator_channel_key: str,
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
    repo_id = binding.get("repo_id") if isinstance(binding, dict) else None
    resource_kind = binding.get("resource_kind") if isinstance(binding, dict) else None
    resource_id = binding.get("resource_id") if isinstance(binding, dict) else None
    orchestration_service, thread = resolve_discord_thread_target(
        service,
        channel_id=channel_id,
        workspace_root=workspace_root,
        agent=agent,
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
    if (
        str(getattr(started_execution.execution, "status", "") or "").strip()
        == "queued"
    ):
        _ensure_discord_thread_queue_worker(
            service,
            orchestration_service=orchestration_service,
            managed_thread_id=thread.thread_target_id,
            channel_id=channel_id,
            public_execution_error=public_execution_error,
            timeout_error=timeout_error,
            interrupted_error=interrupted_error,
        )
        return DiscordMessageTurnResult(
            final_message="Queued (waiting for available worker...)"
        )

    max_progress_len = max(int(service._config.max_message_length), 32)
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
        except Exception:
            progress_last_updated = now
            return
        progress_rendered = content
        progress_last_updated = now

    async def _progress_heartbeat() -> None:
        while True:
            await asyncio.sleep(heartbeat_interval_seconds)
            await _edit_progress()

    try:
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
    except Exception:
        progress_message_id = None

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
        if progress_heartbeat_task is not None:
            progress_heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await progress_heartbeat_task

    _ensure_discord_thread_queue_worker(
        service,
        orchestration_service=orchestration_service,
        managed_thread_id=thread.thread_target_id,
        channel_id=channel_id,
        public_execution_error=public_execution_error,
        timeout_error=timeout_error,
        interrupted_error=interrupted_error,
    )
    if finalized["status"] != "ok":
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
        except Exception:
            pass
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
    agent: str,
    model_override: Optional[str],
    reasoning_effort: Optional[str],
    session_key: str,
    orchestrator_channel_key: str,
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
        agent=agent,
        model_override=model_override,
        reasoning_effort=reasoning_effort,
        session_key=session_key,
        orchestrator_channel_key=orchestrator_channel_key,
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
