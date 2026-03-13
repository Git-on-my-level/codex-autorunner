from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from ...core.context_awareness import (
    maybe_inject_car_awareness,
    maybe_inject_prompt_writing_hint,
)
from ...core.pma_context import build_hub_snapshot, format_pma_prompt, load_pma_prompt
from ...core.ports.run_event import (
    RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
    RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
    RUN_EVENT_DELTA_TYPE_LOG_LINE,
    RUN_EVENT_DELTA_TYPE_USER_MESSAGE,
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
from ...integrations.chat.collaboration_policy import CollaborationEvaluationResult
from ...integrations.chat.compaction import match_pending_compact_seed
from ...integrations.chat.dispatcher import DispatchContext
from ...integrations.chat.models import ChatMessageEvent
from ..chat.progress_primitives import TurnProgressTracker, render_progress_text
from ..chat.turn_metrics import (
    _extract_context_usage_percent,
    _format_turn_metrics,
)
from .components import build_cancel_turn_button
from .rendering import (
    chunk_discord_message,
    format_discord_message,
    truncate_for_discord,
)


@dataclass(frozen=True)
class DiscordMessageTurnResult:
    final_message: str
    preview_message_id: Optional[str] = None
    token_usage: Optional[dict[str, Any]] = None
    elapsed_seconds: Optional[float] = None


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

    if not pma_enabled:
        paused = await service._find_paused_flow_run(workspace_root)
        if paused is not None:
            if service._is_user_ticket_pause(workspace_root, paused):
                log_event_fn(
                    service._logger,
                    logging.INFO,
                    "discord.flow.reply.skipped_for_user_ticket_pause",
                    channel_id=channel_id,
                    run_id=paused.id,
                )
            else:
                reply_text = text
                if has_attachments:
                    (
                        reply_text,
                        saved_attachments,
                        failed_attachments,
                        transcript_message,
                        _native_input_items,
                    ) = await service._with_attachment_context(
                        prompt_text=text,
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
                                    "Failed to download attachments from Discord. "
                                    "Please retry."
                                ),
                            },
                        )
                        return

                reply_path = service._write_user_reply(
                    workspace_root, paused, reply_text
                )
                run_mirror = service._flow_run_mirror(workspace_root)
                run_mirror.mirror_inbound(
                    run_id=paused.id,
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
                    updated = await controller.resume_flow(paused.id)
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
                await service._send_channel_message_safe(
                    channel_id,
                    {"content": content},
                )
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
                return

    prompt_text = text
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
                    "content": "Failed to download attachments from Discord. Please retry.",
                },
            )
        return

    if not pma_enabled:
        prompt_text, injected = maybe_inject_car_awareness(prompt_text)
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
            return

    prompt_text, _github_injected = await service._maybe_inject_github_context(
        prompt_text,
        workspace_root,
        link_source_text=text,
        allow_cross_repo=pma_enabled,
    )

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
    apply_pending_compact = pending_compact_seed is not None
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
        turn_result = await service._run_agent_turn_for_message(**run_turn_kwargs)
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
            {"content": f"Turn failed: {exc} (conversation {context.conversation_id})"},
        )
        return

    if isinstance(turn_result, DiscordMessageTurnResult):
        response_text = turn_result.final_message
        preview_message_id = turn_result.preview_message_id
        metrics_text = _format_turn_metrics(
            turn_result.token_usage,
            turn_result.elapsed_seconds,
        )
        if metrics_text:
            if response_text.strip():
                response_text = f"{response_text}\n\n{metrics_text}"
            else:
                response_text = f"(No response text returned.)\n\n{metrics_text}"
    else:
        response_text = str(turn_result or "")
        preview_message_id = None

    chunks = chunk_discord_message(
        response_text or "(No response text returned.)",
        max_len=service._config.max_message_length,
        with_numbering=False,
    )
    if not chunks:
        chunks = ["(No response text returned.)"]
    for idx, chunk in enumerate(chunks, 1):
        await service._send_channel_message_safe(
            channel_id,
            {"content": chunk},
            record_id=f"turn:{session_key}:{idx}:{uuid.uuid4().hex[:8]}",
        )
    if isinstance(preview_message_id, str) and preview_message_id:
        await service._delete_channel_message_safe(
            channel_id=channel_id,
            message_id=preview_message_id,
            record_id=f"turn:delete_progress:{session_key}:{uuid.uuid4().hex[:8]}",
        )
    if apply_pending_compact:
        await service._store.clear_pending_compact_seed(channel_id=channel_id)
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
    orchestrator = await service._orchestrator_for_workspace(
        workspace_root, channel_id=orchestrator_channel_key
    )
    progress_channel_id = (
        orchestrator_channel_key.split(":", 1)[1]
        if orchestrator_channel_key.startswith("pma:")
        else orchestrator_channel_key
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
    progress_failure_count = 0
    progress_heartbeat_task: Optional[asyncio.Task[None]] = None
    active_progress_labels = {"working", "queued", "running", "review"}

    async def _edit_progress(
        *,
        force: bool = False,
        remove_components: bool = False,
        render_mode: str = "live",
    ) -> None:
        nonlocal progress_rendered
        nonlocal progress_last_updated
        nonlocal progress_failure_count
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
                channel_id=progress_channel_id,
                message_id=progress_message_id,
                payload=payload,
            )
        except Exception as exc:
            log_event_fn(
                service._logger,
                logging.WARNING,
                "discord.turn.progress.edit_failed",
                channel_id=progress_channel_id,
                message_id=progress_message_id,
                failure_count=progress_failure_count + 1,
                exc=exc,
            )
            progress_failure_count += 1
            progress_last_updated = now
            return
        progress_failure_count = 0
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
            initial_rendered, max_len=max_progress_len
        )
        response = await service._send_channel_message(
            progress_channel_id,
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
    except Exception as exc:
        log_event_fn(
            service._logger,
            logging.WARNING,
            "discord.turn.progress.placeholder_failed",
            channel_id=progress_channel_id,
            exc=exc,
        )

    state = service._build_runner_state(
        agent=agent,
        model_override=model_override,
        reasoning_effort=reasoning_effort,
    )
    known_session = orchestrator.get_thread_id(session_key)
    final_message = ""
    assistant_stream_fallback = ""
    completed_seen = False
    token_usage: Optional[dict[str, Any]] = None
    error_message = None
    session_from_events = known_session

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

    def _progress_item_id_for_log_line(content: str) -> Optional[str]:
        normalized = " ".join(content.split()).strip().lower()
        if normalized.startswith("tokens used"):
            return "opencode:token-usage"
        if normalized.startswith("context window:"):
            return "opencode:context-window"
        return None

    try:
        async for run_event in orchestrator.run_turn(
            agent_id=agent,
            state=state,
            prompt=prompt_text,
            input_items=input_items,
            model=model_override,
            reasoning=reasoning_effort,
            session_key=session_key,
            session_id=known_session,
            workspace_root=workspace_root,
        ):
            if isinstance(run_event, Started):
                if isinstance(run_event.session_id, str) and run_event.session_id:
                    session_from_events = run_event.session_id
            elif isinstance(run_event, OutputDelta):
                if run_event.delta_type == RUN_EVENT_DELTA_TYPE_USER_MESSAGE:
                    continue
                if (
                    run_event.delta_type
                    in {
                        RUN_EVENT_DELTA_TYPE_ASSISTANT_STREAM,
                        RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE,
                    }
                    and isinstance(run_event.content, str)
                    and run_event.content
                ):
                    assistant_stream_fallback = _merge_assistant_stream(
                        assistant_stream_fallback,
                        run_event.content,
                    )
                if isinstance(run_event.content, str) and run_event.content.strip():
                    if run_event.delta_type == RUN_EVENT_DELTA_TYPE_ASSISTANT_MESSAGE:
                        latest_output = tracker.latest_output_text().strip()
                        incoming_output = run_event.content.strip()
                        if latest_output and (
                            incoming_output == latest_output
                            or incoming_output.startswith(latest_output)
                        ):
                            tracker.note_output(run_event.content)
                        else:
                            tracker.note_output(
                                run_event.content,
                                new_segment=True,
                            )
                        tracker.end_output_segment()
                    elif run_event.delta_type == RUN_EVENT_DELTA_TYPE_LOG_LINE:
                        item_id = _progress_item_id_for_log_line(run_event.content)
                        if item_id:
                            if not tracker.update_action_by_item_id(
                                item_id,
                                run_event.content,
                                "update",
                                label="output",
                            ):
                                tracker.add_action(
                                    "output",
                                    run_event.content,
                                    "update",
                                    item_id=item_id,
                                    normalize_text=False,
                                )
                        else:
                            tracker.note_output(
                                run_event.content,
                                new_segment=True,
                            )
                            tracker.end_output_segment()
                    else:
                        tracker.note_output(run_event.content)
                    await _edit_progress()
            elif isinstance(run_event, ToolCall):
                tool_name = run_event.tool_name.strip() if run_event.tool_name else ""
                tracker.note_tool(tool_name or "Tool call")
                await _edit_progress()
            elif isinstance(run_event, ApprovalRequested):
                summary = run_event.description.strip() if run_event.description else ""
                tracker.note_approval(summary or "Approval requested")
                await _edit_progress()
            elif isinstance(run_event, RunNotice):
                notice = run_event.message.strip() if run_event.message else ""
                if not notice:
                    notice = run_event.kind.strip() if run_event.kind else "notice"
                if run_event.kind in {"thinking", "reasoning"}:
                    tracker.note_thinking(notice)
                else:
                    tracker.add_action("notice", notice, "update")
                await _edit_progress()
            elif isinstance(run_event, TokenUsage):
                usage_payload = run_event.usage
                if isinstance(usage_payload, dict):
                    token_usage = usage_payload
                    tracker.context_usage_percent = _extract_context_usage_percent(
                        usage_payload
                    )
            elif isinstance(run_event, Completed):
                final_message = run_event.final_message or final_message
                if final_message.strip():
                    tracker.drop_terminal_output_if_duplicate(final_message)
                completed_seen = True
                tracker.clear_transient_action()
                tracker.set_label("done")
                await _edit_progress(
                    force=True,
                    remove_components=True,
                    render_mode="final",
                )
            elif isinstance(run_event, Failed):
                failed_message = run_event.error_message or "Turn failed"
                if completed_seen:
                    log_event_fn(
                        service._logger,
                        logging.WARNING,
                        "discord.turn.failed_late_ignored",
                        channel_id=progress_channel_id,
                        session_key=session_key,
                        error_message=failed_message,
                        final_message_length=len(final_message),
                        fallback_stream_length=len(assistant_stream_fallback),
                    )
                    tracker.clear_transient_action()
                    tracker.set_label("done")
                    await _edit_progress(
                        force=True,
                        remove_components=True,
                        render_mode="final",
                    )
                    continue
                error_message = failed_message
                tracker.note_error(error_message)
                tracker.clear_transient_action()
                tracker.set_label("failed")
                await _edit_progress(force=True, remove_components=True)
    except Exception as exc:
        error_message = str(exc) or "Turn failed"
        tracker.note_error(error_message)
        tracker.clear_transient_action()
        tracker.set_label("failed")
        await _edit_progress(force=True, remove_components=True)
        raise
    finally:
        if progress_heartbeat_task is not None:
            progress_heartbeat_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await progress_heartbeat_task
    if not error_message and not completed_seen:
        tracker.clear_transient_action()
        tracker.set_label("done")
        await _edit_progress(
            force=True,
            remove_components=True,
            render_mode="final",
        )
    if session_from_events:
        orchestrator.set_thread_id(session_key, session_from_events)
    if error_message:
        raise RuntimeError(error_message)
    if not final_message.strip() and assistant_stream_fallback.strip():
        final_message = assistant_stream_fallback
        log_event_fn(
            service._logger,
            logging.INFO,
            "discord.turn.final_message.fallback_stream",
            channel_id=progress_channel_id,
            session_key=session_key,
            fallback_length=len(final_message),
        )
    elapsed_seconds = max(0.0, time.monotonic() - tracker.started_at)
    return DiscordMessageTurnResult(
        final_message=final_message,
        preview_message_id=progress_message_id,
        token_usage=token_usage,
        elapsed_seconds=elapsed_seconds,
    )
