from __future__ import annotations

import contextlib
import logging
import uuid
from pathlib import Path
from typing import Any, Optional

from ....core.orchestration.transcript_mirror import TranscriptMirrorStore
from ....core.pma_thread_store import PmaThreadStore
from ....core.utils import canonicalize_path
from ...chat.compaction import build_compact_seed_prompt
from ..errors import DiscordAPIError
from ..interaction_runtime import ensure_ephemeral_response_deferred
from ..rendering import (
    chunk_discord_message,
)

COMPACT_SUMMARY_PROMPT = (
    "Summarize the conversation so far into a concise context block I can paste into "
    "a new thread. Include goals, constraints, decisions, and current state."
)
COMPACT_FALLBACK_HISTORY_LIMIT = 5
COMPACT_FALLBACK_PREVIEW_CHARS = 600


def _build_fallback_compact_summary(
    service: Any,
    *,
    thread_target_id: str,
) -> Optional[str]:
    transcript_entries = TranscriptMirrorStore(
        service._config.root
    ).list_target_history(
        target_kind="thread_target",
        target_id=thread_target_id,
        limit=COMPACT_FALLBACK_HISTORY_LIMIT,
    )
    sections: list[str] = []
    for index, entry in enumerate(reversed(transcript_entries), start=1):
        preview = str(entry.get("preview") or entry.get("content") or "").strip()
        if not preview:
            continue
        if len(preview) > COMPACT_FALLBACK_PREVIEW_CHARS:
            preview = preview[: COMPACT_FALLBACK_PREVIEW_CHARS - 3].rstrip() + "..."
        sections.append(f"Recent turn {index}:\n{preview}")
    if not sections:
        return None
    return (
        "Fallback context recovered from recent Discord thread transcripts.\n\n"
        + "\n\n".join(sections)
    )


async def handle_car_compact(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
) -> None:
    from ..service import log_event

    async def _finish_compact_interaction(text: str) -> None:
        await service.send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=text,
        )

    binding = await service._store.get_binding(channel_id=channel_id)
    if binding is None:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "Channel not bound. Use /car bind first.",
        )
        return

    workspace_raw = binding.get("workspace_path")
    workspace_root: Optional[Path] = None
    if isinstance(workspace_raw, str) and workspace_raw.strip():
        candidate = canonicalize_path(Path(workspace_raw))
        if candidate.exists() and candidate.is_dir():
            workspace_root = candidate

    pma_enabled = bool(binding.get("pma_enabled", False))
    if workspace_root is None and pma_enabled:
        fallback = canonicalize_path(Path(service._config.root))
        if fallback.exists() and fallback.is_dir():
            workspace_root = fallback

    if workspace_root is None:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "Binding is invalid. Run /car bind first.",
        )
        return

    agent, agent_profile = service._resolve_agent_state(binding)
    model_override = binding.get("model_override")
    if not isinstance(model_override, str) or not model_override.strip():
        model_override = None
    reasoning_effort = binding.get("reasoning_effort")
    if not isinstance(reasoning_effort, str) or not reasoning_effort.strip():
        reasoning_effort = None

    mode = "pma" if pma_enabled else "repo"
    _orchestration_service, _binding_row, current_thread = (
        service._get_discord_thread_binding(channel_id=channel_id, mode=mode)
    )
    lifecycle_status = (
        str(getattr(current_thread, "lifecycle_status", "") or "").strip().lower()
        if current_thread is not None
        else ""
    )
    if current_thread is None or lifecycle_status != "active":
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "No active session to compact. Send a message first to start a conversation.",
        )
        return

    deferred = await ensure_ephemeral_response_deferred(
        service,
        interaction_id,
        interaction_token,
    )

    interaction_text: Optional[str] = None
    previous_thread_id = current_thread.thread_target_id
    next_thread_id: Optional[str] = None
    try:
        try:
            turn_result = await service._run_agent_turn_for_message(
                workspace_root=workspace_root,
                prompt_text=COMPACT_SUMMARY_PROMPT,
                agent=agent,
                model_override=model_override,
                reasoning_effort=reasoning_effort,
                session_key=current_thread.thread_target_id,
                orchestrator_channel_key=(
                    channel_id if not pma_enabled else f"pma:{channel_id}"
                ),
            )
        except Exception as exc:  # intentional: agent turn failures are unpredictable
            log_event(
                service._logger,
                logging.WARNING,
                "discord.compact.turn_failed",
                channel_id=channel_id,
                workspace_root=str(workspace_root),
                exc=exc,
            )
            await service._send_channel_message_safe(
                channel_id,
                {"content": f"Compact failed: {exc}"},
            )
            interaction_text = (
                "Compaction failed. Check the channel for the error message."
            )
            return

        response_text = (
            turn_result.final_message.strip() if turn_result.final_message else ""
        )
        preview_message_id = (
            turn_result.preview_message_id
            if isinstance(turn_result.preview_message_id, str)
            and turn_result.preview_message_id
            else None
        )
        if not response_text:
            response_text = (
                _build_fallback_compact_summary(
                    service,
                    thread_target_id=previous_thread_id,
                )
                or ""
            )
        if not response_text:
            log_event(
                service._logger,
                logging.WARNING,
                "discord.compact.empty_summary",
                channel_id=channel_id,
                workspace_root=str(workspace_root),
                previous_thread_id=previous_thread_id,
            )
            if preview_message_id:
                await service._delete_channel_message_safe(
                    channel_id=channel_id,
                    message_id=preview_message_id,
                    record_id=(
                        "compact:delete_progress:"
                        f"{previous_thread_id}:{uuid.uuid4().hex[:8]}"
                    ),
                )
            await service._send_channel_message_safe(
                channel_id,
                {
                    "content": (
                        "Compaction returned an empty summary. Kept the current "
                        "session active; please retry."
                    )
                },
            )
            interaction_text = (
                "Compaction returned an empty summary. Kept the current session "
                "active; please retry."
            )
            return
        if not (turn_result.final_message or "").strip():
            log_event(
                service._logger,
                logging.INFO,
                "discord.compact.transcript_fallback_used",
                channel_id=channel_id,
                workspace_root=str(workspace_root),
                previous_thread_id=previous_thread_id,
            )
        try:
            (
                _had_previous,
                next_thread_id,
            ) = await service._reset_discord_thread_binding(
                channel_id=channel_id,
                workspace_root=workspace_root,
                agent=agent,
                agent_profile=agent_profile,
                repo_id=(
                    str(binding.get("repo_id")).strip()
                    if isinstance(binding.get("repo_id"), str)
                    and str(binding.get("repo_id")).strip()
                    else None
                ),
                resource_kind=(
                    str(binding.get("resource_kind")).strip()
                    if isinstance(binding.get("resource_kind"), str)
                    and str(binding.get("resource_kind")).strip()
                    else None
                ),
                resource_id=(
                    str(binding.get("resource_id")).strip()
                    if isinstance(binding.get("resource_id"), str)
                    and str(binding.get("resource_id")).strip()
                    else None
                ),
                pma_enabled=pma_enabled,
            )
        except (
            Exception
        ) as exc:  # intentional: thread reset involves multiple Discord API calls
            log_event(
                service._logger,
                logging.WARNING,
                "discord.compact.reset_failed",
                channel_id=channel_id,
                workspace_root=str(workspace_root),
                exc=exc,
            )
            await service._send_channel_message_safe(
                channel_id,
                {
                    "content": "Compact summary generated, but starting a fresh thread failed."
                },
            )
            interaction_text = (
                "Compaction generated a summary, but starting a fresh thread failed."
            )
            return
        compact_pending_session_key = service._build_message_session_key(
            channel_id=channel_id,
            workspace_root=workspace_root,
            pma_enabled=pma_enabled,
            agent=agent,
            agent_profile=agent_profile,
        )
        try:
            PmaThreadStore(service._config.root).set_thread_compact_seed(
                next_thread_id,
                response_text,
            )
            await service._store.set_pending_compact_seed(
                channel_id=channel_id,
                seed_text=build_compact_seed_prompt(response_text),
                session_key=compact_pending_session_key,
            )
        except (
            Exception
        ) as exc:  # intentional: store write can fail in various database-specific ways
            log_event(
                service._logger,
                logging.WARNING,
                "discord.compact.seed_save_failed",
                channel_id=channel_id,
                workspace_root=str(workspace_root),
                next_thread_id=next_thread_id,
                previous_thread_id=previous_thread_id,
                exc=exc,
            )
            rollback_failed = False
            try:
                orchestration_service = service._discord_thread_service()
                if next_thread_id:
                    with contextlib.suppress(
                        Exception
                    ):  # intentional: best-effort archive during rollback
                        orchestration_service.archive_thread_target(next_thread_id)
                restored = orchestration_service.resume_thread_target(
                    previous_thread_id
                )
                service._attach_discord_thread_binding(
                    channel_id=channel_id,
                    thread_target_id=restored.thread_target_id,
                    agent=agent,
                    agent_profile=agent_profile,
                    repo_id=(
                        str(binding.get("repo_id")).strip()
                        if isinstance(binding.get("repo_id"), str)
                        and str(binding.get("repo_id")).strip()
                        else None
                    ),
                    resource_kind=(
                        str(binding.get("resource_kind")).strip()
                        if isinstance(binding.get("resource_kind"), str)
                        and str(binding.get("resource_kind")).strip()
                        else None
                    ),
                    resource_id=(
                        str(binding.get("resource_id")).strip()
                        if isinstance(binding.get("resource_id"), str)
                        and str(binding.get("resource_id")).strip()
                        else None
                    ),
                    workspace_root=workspace_root,
                    pma_enabled=pma_enabled,
                )
            except (
                Exception
            ) as rollback_exc:  # intentional: rollback involves multiple recovery operations
                rollback_failed = True
                log_event(
                    service._logger,
                    logging.ERROR,
                    "discord.compact.seed_save_rollback_failed",
                    channel_id=channel_id,
                    workspace_root=str(workspace_root),
                    next_thread_id=next_thread_id,
                    previous_thread_id=previous_thread_id,
                    exc=rollback_exc,
                )
            await service._send_channel_message_safe(
                channel_id,
                {
                    "content": (
                        "Compaction summary generated, but saving the compacted context failed. "
                        "Restored the previous thread; please retry."
                        if not rollback_failed
                        else "Compaction summary generated, but saving the compacted context failed and restoring the previous thread also failed."
                    )
                },
            )
            interaction_text = (
                "Compaction summary generated, but saving the compacted context failed. "
                "Restored the previous thread; please retry."
                if not rollback_failed
                else "Compaction summary generated, but saving the compacted context failed and restoring the previous thread also failed."
            )
            return

        try:
            chunks = chunk_discord_message(
                f"**Conversation Summary:**\n\n{response_text}",
                max_len=service._config.max_message_length,
                with_numbering=False,
            )
            if not chunks:
                chunks = ["**Conversation Summary:**\n\n(No summary generated.)"]

            next_chunk_index = 0
            preview_chunk_applied = False

            if preview_message_id:
                try:
                    preview_payload: dict[str, Any] = {"content": chunks[0]}
                    await service._rest.edit_channel_message(
                        channel_id=channel_id,
                        message_id=preview_message_id,
                        payload=preview_payload,
                    )
                    preview_chunk_applied = True
                    next_chunk_index = 1
                except (DiscordAPIError, OSError) as exc:
                    log_event(
                        service._logger,
                        logging.WARNING,
                        "discord.compact.preview_edit_failed",
                        channel_id=channel_id,
                        message_id=preview_message_id,
                        exc=exc,
                    )

            if not preview_chunk_applied:
                first_payload: dict[str, Any] = {"content": chunks[0]}
                await service._send_channel_message_safe(
                    channel_id,
                    first_payload,
                )
                next_chunk_index = 1

            for chunk in chunks[next_chunk_index:]:
                payload: dict[str, Any] = {"content": chunk}
                await service._send_channel_message_safe(channel_id, payload)
            await service._flush_outbox_files(
                workspace_root=workspace_root,
                channel_id=channel_id,
            )
        except (
            Exception
        ) as exc:  # intentional: finalization mixes Discord API + file I/O
            log_event(
                service._logger,
                logging.WARNING,
                "discord.compact.finalize_failed",
                channel_id=channel_id,
                workspace_root=str(workspace_root),
                exc=exc,
            )
            await service._send_channel_message_safe(
                channel_id,
                {
                    "content": "Compaction summary posted, but finalizing the fresh thread failed."
                },
            )
            interaction_text = (
                "Compaction summary posted, but finalizing the fresh thread failed."
            )
            return
        interaction_text = (
            "Compaction complete. Summary posted in the channel. "
            "Send your next message to continue this session."
        )
    finally:
        if interaction_text:
            await _finish_compact_interaction(interaction_text)
