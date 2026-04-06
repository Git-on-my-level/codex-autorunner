from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import Any, Optional

from ...chat.approval_modes import APPROVAL_MODE_USAGE, normalize_approval_mode
from ..components import (
    DISCORD_SELECT_OPTION_MAX_OPTIONS,
    build_review_commit_picker,
)
from ..message_turns import DiscordMessageTurnResult
from ..rendering import chunk_discord_message

REVIEW_COMMIT_SELECT_ID = "review_commit_select"


async def handle_car_review(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    workspace_root: Path,
    options: dict[str, Any],
) -> None:
    from ..service import log_event

    binding = await service._store.get_binding(channel_id=channel_id)
    if binding is None:
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            "Channel binding not found.",
        )
        return

    current_agent, current_profile = service._resolve_agent_state(binding)

    supports_review = service._agent_supports_capability(current_agent, "review")
    if not supports_review:
        supported_agents = service._agents_supporting_capability("review")
        supported_hint = (
            f" Switch to an agent with review support: {', '.join(supported_agents)}."
            if supported_agents
            else ""
        )
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"{service._agent_display_name(current_agent)} does not support code review in Discord.{supported_hint}",
        )
        return

    deferred = await service._defer_ephemeral(
        interaction_id=interaction_id,
        interaction_token=interaction_token,
    )
    target_arg = options.get("target", "")
    target_type = "uncommittedChanges"
    target_value: Optional[str] = None
    prompt_commit_picker = False

    if isinstance(target_arg, str) and target_arg.strip():
        target_text = target_arg.strip()
        target_lower = target_text.lower()
        if target_lower.startswith("base "):
            branch = target_text[5:].strip()
            if branch:
                target_type = "baseBranch"
                target_value = branch
        elif target_lower == "commit" or target_lower.startswith("commit "):
            sha = target_text[6:].strip()
            if sha:
                commits = await service._list_recent_commits_for_picker(workspace_root)
                commit_subjects = {
                    commit_sha: subject for commit_sha, subject in commits
                }
                search_items = [(commit_sha, commit_sha) for commit_sha, _ in commits]
                aliases = {
                    commit_sha: (subject,) if subject else ()
                    for commit_sha, subject in commits
                }

                async def _prompt_commit_matches(
                    query_text: str,
                    filtered_search_items: list[tuple[str, str]],
                ) -> None:
                    filtered_commits = [
                        (commit_sha, commit_subjects.get(commit_sha, ""))
                        for commit_sha, _label in filtered_search_items
                    ]
                    await service._send_or_respond_with_components_ephemeral(
                        interaction_id=interaction_id,
                        interaction_token=interaction_token,
                        deferred=deferred,
                        text=(
                            f"Matched {len(filtered_commits)} commits for `{query_text}`. "
                            "Select a commit to review:"
                        ),
                        components=[
                            build_review_commit_picker(
                                filtered_commits,
                                custom_id=REVIEW_COMMIT_SELECT_ID,
                            )
                        ],
                    )

                resolved_commit = await service._resolve_picker_query_or_prompt(
                    query=sha,
                    items=search_items,
                    limit=DISCORD_SELECT_OPTION_MAX_OPTIONS,
                    aliases=aliases,
                    prompt_filtered_items=_prompt_commit_matches,
                )
                if resolved_commit is not None:
                    target_type = "commit"
                    target_value = resolved_commit
                elif commits:
                    return
                else:
                    target_type = "commit"
                    target_value = sha
            else:
                prompt_commit_picker = True
        elif target_lower in ("uncommitted", ""):
            pass
        elif target_lower == "custom":
            await service._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=(
                    "Provide custom review instructions after `custom`, for example: "
                    "`/car review target:custom focus on security regressions`."
                ),
            )
            return
        elif target_lower.startswith("custom "):
            custom_instructions = target_text[7:].strip()
            if not custom_instructions:
                await service._send_or_respond_ephemeral(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    deferred=deferred,
                    text=(
                        "Provide custom review instructions after `custom`, for example: "
                        "`/car review target:custom focus on security regressions`."
                    ),
                )
                return
            target_type = "custom"
            target_value = custom_instructions
        else:
            target_type = "custom"
            target_value = target_text

    if prompt_commit_picker:
        commits = await service._list_recent_commits_for_picker(workspace_root)
        if not commits:
            await service._send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text="No recent commits found. Use `/car review target:commit <sha>`.",
            )
            return
        await service._send_or_respond_with_components_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text="Select a commit to review:",
            components=[
                build_review_commit_picker(commits, custom_id=REVIEW_COMMIT_SELECT_ID)
            ],
        )
        return

    prompt_parts: list[str] = ["Please perform a code review."]
    if target_type == "uncommittedChanges":
        prompt_parts.append(
            "Review the uncommitted changes in the working directory. "
            "Use `git diff` to see what has changed and provide feedback."
        )
    elif target_type == "baseBranch" and target_value:
        prompt_parts.append(
            f"Review the changes compared to the base branch `{target_value}`. "
            f"Use `git diff {target_value}...HEAD` to see the diff and provide feedback."
        )
    elif target_type == "commit" and target_value:
        prompt_parts.append(
            f"Review the commit `{target_value}`. "
            f"Use `git show {target_value}` to see the changes and provide feedback."
        )
    elif target_type == "custom" and target_value:
        prompt_parts.append(f"Review instructions: {target_value}")

    prompt_parts.append(
        "\n\nProvide actionable feedback focusing on: bugs, security issues, "
        "performance problems, and significant code quality issues. "
        "Include file paths and line numbers where relevant."
    )
    prompt_text = "\n".join(prompt_parts)

    agent, agent_profile = service._resolve_agent_state(binding)
    model_override = binding.get("model_override")
    if not isinstance(model_override, str) or not model_override.strip():
        model_override = None
    reasoning_effort = binding.get("reasoning_effort")
    if not isinstance(reasoning_effort, str) or not reasoning_effort.strip():
        reasoning_effort = None

    session_key = service._build_message_session_key(
        channel_id=channel_id,
        workspace_root=workspace_root,
        pma_enabled=False,
        agent=agent,
        agent_profile=agent_profile,
    )

    log_event(
        service._logger,
        logging.INFO,
        "discord.review.starting",
        channel_id=channel_id,
        workspace_root=str(workspace_root),
        target_type=target_type,
        target_value=target_value,
        agent=agent,
        agent_profile=agent_profile,
    )

    try:
        turn_result = await service._run_agent_turn_for_message(
            workspace_root=workspace_root,
            prompt_text=prompt_text,
            agent=agent,
            model_override=model_override,
            reasoning_effort=reasoning_effort,
            session_key=session_key,
            orchestrator_channel_key=channel_id,
        )
    except Exception as exc:  # intentional: top-level agent turn fault barrier
        log_event(
            service._logger,
            logging.WARNING,
            "discord.review.failed",
            channel_id=channel_id,
            workspace_root=str(workspace_root),
            target_type=target_type,
            exc=exc,
        )
        await service._send_channel_message_safe(
            channel_id,
            {"content": f"Review failed: {exc}"},
        )
        return

    if isinstance(turn_result, DiscordMessageTurnResult):
        response_text = turn_result.final_message
        preview_message_id = turn_result.preview_message_id
    else:
        response_text = str(turn_result or "")
        preview_message_id = None

    if not response_text or not response_text.strip():
        response_text = "(Review completed with no output.)"

    chunks = chunk_discord_message(
        response_text,
        max_len=service._config.max_message_length,
        with_numbering=False,
    )
    if not chunks:
        chunks = ["(Review completed with no output.)"]

    for idx, chunk in enumerate(chunks, 1):
        await service._send_channel_message_safe(
            channel_id,
            {"content": chunk},
            record_id=f"review:{session_key}:{idx}:{uuid.uuid4().hex[:8]}",
        )
    if isinstance(preview_message_id, str) and preview_message_id:
        await service._delete_channel_message_safe(
            channel_id=channel_id,
            message_id=preview_message_id,
            record_id=(f"review:delete_progress:{session_key}:{uuid.uuid4().hex[:8]}"),
        )

    try:
        await service._flush_outbox_files(
            workspace_root=workspace_root,
            channel_id=channel_id,
        )
    except Exception as exc:  # intentional: best-effort outbox flush after review
        log_event(
            service._logger,
            logging.WARNING,
            "discord.review.outbox_flush_failed",
            channel_id=channel_id,
            target_type=target_type,
            message_id=preview_message_id,
            exc=exc,
        )
    log_event(
        service._logger,
        logging.INFO,
        "discord.review.completed",
        channel_id=channel_id,
        target_type=target_type,
        chunk_count=len(chunks),
    )


async def handle_car_approvals(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    options: dict[str, Any],
) -> None:
    mode = options.get("mode", "")
    if not isinstance(mode, str):
        mode = ""
    mode = mode.strip().lower()

    binding = await service._store.get_binding(channel_id=channel_id)
    if binding is None:
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            "Channel not bound. Use /car bind first.",
        )
        return

    if not mode:
        current_mode = binding.get("approval_mode", "yolo")
        approval_policy = binding.get("approval_policy", "default")
        sandbox_policy = binding.get("sandbox_policy", "default")

        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            "\n".join(
                [
                    f"Approval mode: {current_mode}",
                    f"Approval policy: {approval_policy}",
                    f"Sandbox policy: {sandbox_policy}",
                    "",
                    f"Usage: /car approvals {APPROVAL_MODE_USAGE}",
                ]
            ),
        )
        return

    new_mode = normalize_approval_mode(mode, include_command_aliases=True)
    if new_mode is None:
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            f"Unknown mode: {mode}. Valid options: {APPROVAL_MODE_USAGE}",
        )
        return

    await service._store.update_approval_mode(channel_id=channel_id, mode=new_mode)
    await service._respond_ephemeral(
        interaction_id,
        interaction_token,
        f"Approval mode set to {new_mode}.",
    )
