from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from pathlib import Path
from typing import Any, Optional

from ....core.git_utils import (
    GitError,
    clean_untracked_worktree,
    describe_newt_reject_reasons,
    reset_worktree_to_head,
)
from ....core.utils import canonicalize_path
from ...chat.interrupt_controller import (
    SharedInterruptState,
    render_managed_thread_interrupt_message,
    request_managed_thread_interrupt,
)
from ...chat.session_messages import (
    build_fresh_session_started_lines,
    build_thread_detail_lines,
)
from ..components import (
    DISCORD_BUTTON_STYLE_DANGER,
    DISCORD_SELECT_OPTION_MAX_OPTIONS,
    build_action_row,
    build_button,
    build_session_threads_picker,
)
from ..interaction_registry import NEWT_CANCEL_CUSTOM_ID, NEWT_HARD_RESET_CUSTOM_ID
from ..interaction_runtime import (
    defer_and_update_runtime_component_message,
    ensure_component_response_deferred,
    ensure_ephemeral_response_deferred,
    ensure_public_response_deferred,
    send_runtime_components_ephemeral,
)
from ..message_turns import (
    clear_discord_turn_progress_leases,
    clear_discord_turn_progress_reuse,
    request_discord_turn_progress_reuse,
)
from ..rendering import format_discord_message, truncate_for_discord

_logger = logging.getLogger(__name__)


def _newt_branch_name(channel_id: str, workspace_root: Path) -> str:
    safe_channel_id = re.sub(r"[^a-zA-Z0-9]+", "-", channel_id).strip("-")
    if not safe_channel_id:
        safe_channel_id = "channel"
    branch_suffix = hashlib.sha256(str(workspace_root).encode("utf-8")).hexdigest()[:10]
    return f"thread-{safe_channel_id}-{branch_suffix}"


def _newt_workspace_token(workspace_root: Path) -> str:
    return hashlib.sha256(str(workspace_root).encode("utf-8")).hexdigest()[:12]


def _newt_component_custom_id(prefix: str, workspace_root: Path) -> str:
    return f"{prefix}:{_newt_workspace_token(workspace_root)}"


def _parse_newt_component_custom_id(custom_id: str, prefix: str) -> Optional[str]:
    if custom_id == prefix:
        return ""
    if not custom_id.startswith(f"{prefix}:"):
        return None
    token = custom_id.split(":", 1)[1].strip()
    return token or None


def _build_newt_reject_components(workspace_root: Path) -> list[dict[str, Any]]:
    return [
        build_action_row(
            [
                build_button(
                    "Hard reset",
                    _newt_component_custom_id(
                        NEWT_HARD_RESET_CUSTOM_ID, workspace_root
                    ),
                    style=DISCORD_BUTTON_STYLE_DANGER,
                ),
                build_button(
                    "Cancel",
                    _newt_component_custom_id(NEWT_CANCEL_CUSTOM_ID, workspace_root),
                ),
            ]
        )
    ]


def _format_newt_reject_message(reasons: list[str]) -> str:
    lines = [
        "Can't start a fresh `/car newt` yet.",
        "",
        "Why:",
        *[f"- {reason}" for reason in reasons],
        "",
        "Choose **Hard reset** to discard local changes and continue, or **Cancel** to keep them.",
    ]
    return format_discord_message("\n".join(lines))


def _format_newt_hard_reset_progress_message() -> str:
    return format_discord_message(
        "Discarding local changes and starting a fresh `/car newt`..."
    )


async def _send_newt_response(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    deferred: bool,
    text: str,
    component_response: bool,
    components: Optional[list[dict[str, Any]]] = None,
) -> None:
    if component_response:
        await service.send_or_update_component_message(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=text,
            components=components,
        )
        return
    if components:
        await service.send_or_respond_public_with_components(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=text,
            components=components,
        )
        return
    await service.send_or_respond_public(
        interaction_id=interaction_id,
        interaction_token=interaction_token,
        deferred=deferred,
        text=text,
    )


async def _finalize_car_newt(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    deferred: bool,
    binding: dict[str, Any],
    workspace_root: Path,
    pma_enabled: bool,
    branch_name: str,
    default_branch: str,
    component_response: bool = False,
) -> None:
    from ..service import log_event

    setup_command_count = 0
    hub_client = getattr(service, "_hub_client", None)
    if hub_client is not None:
        repo_id_raw = binding.get("repo_id")
        repo_id_hint = (
            repo_id_raw.strip()
            if isinstance(repo_id_raw, str) and repo_id_raw
            else None
        )
        try:
            from ....core.hub_control_plane import (
                WorkspaceSetupCommandRequest as _CPSetupCommandRequest,
            )

            result = await hub_client.run_workspace_setup_commands(
                _CPSetupCommandRequest(
                    workspace_root=str(workspace_root),
                    repo_id_hint=repo_id_hint,
                )
            )
            setup_command_count = result.setup_command_count
        except (
            RuntimeError,
            OSError,
        ) as exc:  # intentional: runs arbitrary setup commands with unpredictable failures
            log_event(
                service._logger,
                logging.WARNING,
                "discord.newt.setup.failed",
                channel_id=channel_id,
                workspace_path=str(workspace_root),
                exc=exc,
            )
            text = format_discord_message(
                f"Reset branch `{branch_name}` to `origin/{default_branch}` but setup commands failed: {exc}"
            )
            await _send_newt_response(
                service,
                interaction_id,
                interaction_token,
                deferred=deferred,
                text=text,
                component_response=component_response,
                components=[] if component_response else None,
            )
            return

    agent, agent_profile = service._resolve_agent_state(binding)
    resource_kind = (
        str(binding.get("resource_kind")).strip()
        if isinstance(binding.get("resource_kind"), str)
        and str(binding.get("resource_kind")).strip()
        else None
    )
    resource_id = (
        str(binding.get("resource_id")).strip()
        if isinstance(binding.get("resource_id"), str)
        and str(binding.get("resource_id")).strip()
        else None
    )

    try:
        had_previous, new_thread_id = await service._reset_discord_thread_binding(
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
            resource_kind=resource_kind,
            resource_id=resource_id,
            pma_enabled=pma_enabled,
        )
    except (
        RuntimeError,
        OSError,
        ValueError,
        TypeError,
    ) as exc:
        log_event(
            service._logger,
            logging.WARNING,
            "discord.newt.thread_reset.failed",
            channel_id=channel_id,
            workspace_root=str(workspace_root),
            agent=agent,
            exc=exc,
        )
        text = format_discord_message(
            "Branch reset succeeded, but starting a fresh session failed."
        )
        await _send_newt_response(
            service,
            interaction_id,
            interaction_token,
            deferred=deferred,
            text=text,
            component_response=component_response,
            components=[] if component_response else None,
        )
        return

    await service._store.clear_pending_compact_seed(channel_id=channel_id)
    mode_label = "PMA" if pma_enabled else "repo"
    state_label = "cleared previous thread" if had_previous else "new thread ready"
    setup_note = (
        f" Ran {setup_command_count} setup command(s)." if setup_command_count else ""
    )
    actor_label = service._format_agent_state(agent, agent_profile)
    text = format_discord_message(
        "\n".join(
            [
                (
                    f"Reset branch `{branch_name}` to `origin/{default_branch}` "
                    f"in current workspace and started fresh {mode_label} session "
                    f"for `{actor_label}` ({state_label}).{setup_note}"
                ),
                *build_thread_detail_lines(
                    thread_id=new_thread_id,
                    workspace_path=str(workspace_root),
                    actor_label=actor_label,
                    model=service._status_model_label(binding),
                    effort=service._status_effort_label(binding, agent),
                ),
            ]
        )
    )
    await _send_newt_response(
        service,
        interaction_id,
        interaction_token,
        deferred=deferred,
        text=text,
        component_response=component_response,
        components=[] if component_response else None,
    )


async def handle_car_new(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
) -> None:
    from ..service import log_event

    deferred = await ensure_public_response_deferred(
        service,
        interaction_id,
        interaction_token,
    )
    binding = await service._store.get_binding(channel_id=channel_id)
    if binding is None:
        text = format_discord_message(
            "This channel is not bound. Run `/car bind path:<...>` first."
        )
        await service.send_or_respond_public(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=text,
        )
        return

    pma_enabled = bool(binding.get("pma_enabled", False))
    workspace_raw = binding.get("workspace_path")
    workspace_root: Optional[Path] = None
    if isinstance(workspace_raw, str) and workspace_raw.strip():
        workspace_root = canonicalize_path(Path(workspace_raw))
        if not workspace_root.exists() or not workspace_root.is_dir():
            workspace_root = None
    if workspace_root is None:
        if pma_enabled:
            workspace_root = canonicalize_path(Path(service._config.root))
        else:
            text = format_discord_message(
                "Binding is invalid. Run `/car bind path:<workspace>`."
            )
            await service.send_or_respond_public(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=text,
            )
            return

    agent, agent_profile = service._resolve_agent_state(binding)
    resource_kind = (
        str(binding.get("resource_kind")).strip()
        if isinstance(binding.get("resource_kind"), str)
        and str(binding.get("resource_kind")).strip()
        else None
    )
    resource_id = (
        str(binding.get("resource_id")).strip()
        if isinstance(binding.get("resource_id"), str)
        and str(binding.get("resource_id")).strip()
        else None
    )

    try:
        had_previous, _new_thread_id = await service._reset_discord_thread_binding(
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
            resource_kind=resource_kind,
            resource_id=resource_id,
            pma_enabled=pma_enabled,
        )
    except (
        RuntimeError,
        OSError,
        ValueError,
        TypeError,
    ) as exc:  # intentional: top-level command handler wrapping complex service reset
        log_event(
            service._logger,
            logging.WARNING,
            "discord.new.reset_failed",
            channel_id=channel_id,
            workspace_root=str(workspace_root),
            agent=agent,
            exc=exc,
        )
        text = format_discord_message("Failed to start a fresh session.")
        await service.send_or_respond_public(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=text,
        )
        return
    await service._store.clear_pending_compact_seed(channel_id=channel_id)
    mode_label = "PMA" if pma_enabled else "repo"
    state_label = "cleared previous thread" if had_previous else "new thread ready"
    actor_label = service._format_agent_state(agent, agent_profile)
    text = format_discord_message(
        "\n".join(
            [
                *build_fresh_session_started_lines(
                    mode_label=mode_label,
                    actor_label=actor_label,
                    state_label=state_label,
                ),
                *build_thread_detail_lines(
                    thread_id=_new_thread_id,
                    workspace_path=str(workspace_root),
                    actor_label=actor_label,
                    model=service._status_model_label(binding),
                    effort=service._status_effort_label(binding, agent),
                ),
            ]
        )
    )
    await service.send_or_respond_public(
        interaction_id=interaction_id,
        interaction_token=interaction_token,
        deferred=deferred,
        text=text,
    )


async def handle_car_newt(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    guild_id: Optional[str],
) -> None:
    from ..service import log_event, reset_branch_from_origin_main

    deferred = await ensure_public_response_deferred(
        service,
        interaction_id,
        interaction_token,
    )
    binding = await service._store.get_binding(channel_id=channel_id)
    if binding is None:
        text = format_discord_message(
            "This channel is not bound. Run `/car bind path:<...>` first."
        )
        await service.send_or_respond_public(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=text,
        )
        return

    pma_enabled = bool(binding.get("pma_enabled", False))
    if pma_enabled:
        text = format_discord_message(
            "/car newt is not available in PMA mode. Use `/car new` instead."
        )
        await service.send_or_respond_public(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=text,
        )
        return

    workspace_raw = binding.get("workspace_path")
    workspace_root: Optional[Path] = None
    if isinstance(workspace_raw, str) and workspace_raw.strip():
        workspace_root = canonicalize_path(Path(workspace_raw))
        if not workspace_root.exists() or not workspace_root.is_dir():
            workspace_root = None
    if workspace_root is None:
        text = format_discord_message(
            "Binding is invalid. Run `/car bind path:<workspace>`."
        )
        await service.send_or_respond_public(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=text,
        )
        return

    branch_name = _newt_branch_name(channel_id, workspace_root)

    try:
        default_branch = await asyncio.to_thread(
            reset_branch_from_origin_main,
            workspace_root,
            branch_name,
        )
    except GitError as exc:
        log_event(
            service._logger,
            logging.WARNING,
            "discord.newt.branch_reset.failed",
            channel_id=channel_id,
            branch=branch_name,
            exc=exc,
        )
        if "working tree has uncommitted changes" in str(exc):
            reasons = describe_newt_reject_reasons(workspace_root)
            if not reasons:
                reasons = ["Local git changes are blocking the reset."]
            await _send_newt_response(
                service,
                interaction_id,
                interaction_token,
                deferred=deferred,
                text=_format_newt_reject_message(reasons),
                component_response=False,
                components=_build_newt_reject_components(workspace_root),
            )
            return
        text = format_discord_message(
            f"Failed to reset branch `{branch_name}` from origin default branch: {exc}"
        )
        await _send_newt_response(
            service,
            interaction_id,
            interaction_token,
            deferred=deferred,
            text=text,
            component_response=False,
        )
        return

    await _finalize_car_newt(
        service,
        interaction_id,
        interaction_token,
        channel_id=channel_id,
        deferred=deferred,
        binding=binding,
        workspace_root=workspace_root,
        pma_enabled=pma_enabled,
        branch_name=branch_name,
        default_branch=default_branch,
    )


async def handle_car_newt_hard_reset(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    expected_workspace_token: Optional[str],
) -> None:
    from ..service import log_event, reset_branch_from_origin_main

    deferred = await ensure_component_response_deferred(
        service,
        interaction_id,
        interaction_token,
    )
    binding = await service._store.get_binding(channel_id=channel_id)
    if binding is None:
        await _send_newt_response(
            service,
            interaction_id,
            interaction_token,
            deferred=deferred,
            text=format_discord_message(
                "This channel is not bound. Run `/car bind path:<...>` first."
            ),
            component_response=True,
            components=[],
        )
        return

    pma_enabled = bool(binding.get("pma_enabled", False))
    if pma_enabled:
        await _send_newt_response(
            service,
            interaction_id,
            interaction_token,
            deferred=deferred,
            text=format_discord_message(
                "/car newt is not available in PMA mode. Use `/car new` instead."
            ),
            component_response=True,
            components=[],
        )
        return

    workspace_raw = binding.get("workspace_path")
    workspace_root: Optional[Path] = None
    if isinstance(workspace_raw, str) and workspace_raw.strip():
        workspace_root = canonicalize_path(Path(workspace_raw))
        if not workspace_root.exists() or not workspace_root.is_dir():
            workspace_root = None
    if workspace_root is None:
        await _send_newt_response(
            service,
            interaction_id,
            interaction_token,
            deferred=deferred,
            text=format_discord_message(
                "Binding is invalid. Run `/car bind path:<workspace>`."
            ),
            component_response=True,
            components=[],
        )
        return
    current_workspace_token = _newt_workspace_token(workspace_root)
    if (
        not expected_workspace_token
        or expected_workspace_token != current_workspace_token
    ):
        await _send_newt_response(
            service,
            interaction_id,
            interaction_token,
            deferred=deferred,
            text=format_discord_message(
                "This `/car newt` action no longer matches the channel's current "
                "workspace binding. Run `/car newt` again from the current workspace."
            ),
            component_response=True,
            components=[],
        )
        return

    deferred = await defer_and_update_runtime_component_message(
        service,
        interaction_id,
        interaction_token,
        _format_newt_hard_reset_progress_message(),
        components=[],
    )
    branch_name = _newt_branch_name(channel_id, workspace_root)
    try:
        await asyncio.to_thread(reset_worktree_to_head, workspace_root)
    except GitError as exc:
        log_event(
            service._logger,
            logging.WARNING,
            "discord.newt.hard_reset_tracked.failed",
            channel_id=channel_id,
            branch=branch_name,
            exc=exc,
        )
        text = format_discord_message(
            "Hard reset did not complete cleanly before `/car newt` was cancelled.\n\n"
            f"Reason: {exc}"
        )
        await _send_newt_response(
            service,
            interaction_id,
            interaction_token,
            deferred=deferred,
            text=text,
            component_response=True,
            components=[],
        )
        return
    try:
        await asyncio.to_thread(clean_untracked_worktree, workspace_root)
    except GitError as exc:
        log_event(
            service._logger,
            logging.WARNING,
            "discord.newt.hard_reset_untracked.failed",
            channel_id=channel_id,
            branch=branch_name,
            exc=exc,
        )
        text = format_discord_message(
            "Tracked changes were discarded, but some untracked paths could not be "
            "removed, so `/car newt` was cancelled.\n\n"
            f"Reason: {exc}"
        )
        await _send_newt_response(
            service,
            interaction_id,
            interaction_token,
            deferred=deferred,
            text=text,
            component_response=True,
            components=[],
        )
        return
    try:
        default_branch = await asyncio.to_thread(
            reset_branch_from_origin_main,
            workspace_root,
            branch_name,
        )
    except GitError as exc:
        log_event(
            service._logger,
            logging.WARNING,
            "discord.newt.branch_reset_after_hard_reset.failed",
            channel_id=channel_id,
            branch=branch_name,
            exc=exc,
        )
        text = format_discord_message(
            "Local changes were discarded, but `/car newt` still failed while "
            "resetting the branch from origin.\n\n"
            f"Reason: {exc}\n\n"
            "You can retry `/car newt` after fixing the git problem."
        )
        await _send_newt_response(
            service,
            interaction_id,
            interaction_token,
            deferred=deferred,
            text=text,
            component_response=True,
            components=[],
        )
        return

    await _finalize_car_newt(
        service,
        interaction_id,
        interaction_token,
        channel_id=channel_id,
        deferred=deferred,
        binding=binding,
        workspace_root=workspace_root,
        pma_enabled=pma_enabled,
        branch_name=branch_name,
        default_branch=default_branch,
        component_response=True,
    )


async def handle_car_newt_cancel(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    expected_workspace_token: Optional[str] = None,
) -> None:
    _ = expected_workspace_token
    deferred = await ensure_component_response_deferred(
        service,
        interaction_id,
        interaction_token,
    )
    await _send_newt_response(
        service,
        interaction_id,
        interaction_token,
        deferred=deferred,
        text=format_discord_message("Cancelled `/car newt`. Kept local changes."),
        component_response=True,
        components=[],
    )


async def handle_car_resume(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    options: dict[str, Any],
) -> None:
    deferred = await ensure_ephemeral_response_deferred(
        service,
        interaction_id,
        interaction_token,
    )
    binding = await service._store.get_binding(channel_id=channel_id)
    if binding is None:
        text = format_discord_message(
            "This channel is not bound. Run `/car bind path:<...>` first."
        )
        await service.send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=text,
        )
        return

    pma_enabled = bool(binding.get("pma_enabled", False))
    workspace_raw = binding.get("workspace_path")
    workspace_root: Optional[Path] = None
    if isinstance(workspace_raw, str) and workspace_raw.strip():
        workspace_root = canonicalize_path(Path(workspace_raw))
        if not workspace_root.exists() or not workspace_root.is_dir():
            workspace_root = None
    if workspace_root is None:
        if pma_enabled:
            workspace_root = canonicalize_path(Path(service._config.root))
        else:
            text = format_discord_message(
                "Binding is invalid. Run `/car bind path:<workspace>`."
            )
            await service.send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=text,
            )
            return

    agent, agent_profile = service._resolve_agent_state(binding)
    repo_id = (
        str(binding.get("repo_id")).strip()
        if isinstance(binding.get("repo_id"), str)
        and str(binding.get("repo_id")).strip()
        else None
    )
    resource_kind = (
        str(binding.get("resource_kind")).strip()
        if isinstance(binding.get("resource_kind"), str)
        and str(binding.get("resource_kind")).strip()
        else None
    )
    resource_id = (
        str(binding.get("resource_id")).strip()
        if isinstance(binding.get("resource_id"), str)
        and str(binding.get("resource_id")).strip()
        else None
    )
    mode = "pma" if pma_enabled else "repo"
    orchestration_service, _current_binding, current_thread = (
        service._get_discord_thread_binding(channel_id=channel_id, mode=mode)
    )

    raw_thread_id = options.get("thread_id")
    thread_id = raw_thread_id.strip() if isinstance(raw_thread_id, str) else None
    current_thread_id = (
        str(getattr(current_thread, "thread_target_id", "") or "").strip() or None
    )

    if thread_id:
        thread_items = service._list_discord_thread_targets_for_picker(
            workspace_root=workspace_root,
            agent=agent,
            agent_profile=agent_profile,
            current_thread_id=current_thread_id,
            mode=mode,
            repo_id=repo_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
        )
        if thread_items:

            async def _prompt_thread_matches(
                query_text: str,
                filtered_items: list[tuple[str, str]],
            ) -> None:
                header = (
                    f"Current thread: `{current_thread_id}`\n\n"
                    if current_thread_id
                    else ""
                )
                await send_runtime_components_ephemeral(
                    service,
                    interaction_id,
                    interaction_token,
                    format_discord_message(
                        header
                        + (
                            f"Matched {len(filtered_items)} threads for "
                            f"`{query_text}`. Choose one thread to resume:"
                        )
                    ),
                    [build_session_threads_picker(filtered_items)],
                )

            resolved_thread_id = await service._resolve_picker_query_or_prompt(
                query=thread_id,
                items=thread_items,
                limit=DISCORD_SELECT_OPTION_MAX_OPTIONS,
                prompt_filtered_items=_prompt_thread_matches,
            )
            if resolved_thread_id is None:
                return
            thread_id = resolved_thread_id
        target_thread = orchestration_service.get_thread_target(thread_id)
        if target_thread is None:
            await service.send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=format_discord_message(
                    f"Unknown thread `{thread_id}` for this workspace."
                ),
            )
            return
        if str(getattr(target_thread, "workspace_root", "") or "").strip() != str(
            workspace_root.resolve()
        ):
            await service.send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=format_discord_message(
                    "Selected thread belongs to a different workspace."
                ),
            )
            return
        if not service._discord_thread_matches_agent(
            target_thread,
            agent=agent,
            agent_profile=agent_profile,
        ):
            await service.send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=format_discord_message(
                    "Selected thread belongs to a different agent. "
                    f"Current agent: `{service._format_agent_state(agent, agent_profile)}`."
                ),
            )
            return
        lifecycle_status = (
            str(getattr(target_thread, "lifecycle_status", "") or "").strip().lower()
        )
        if lifecycle_status and lifecycle_status != "active":
            try:
                orchestration_service.resume_thread_target(thread_id)
            except (RuntimeError, OSError, ValueError, TypeError):
                _logger.debug(
                    "resume_thread_target failed for %s", thread_id, exc_info=True
                )
        service._attach_discord_thread_binding(
            channel_id=channel_id,
            thread_target_id=thread_id,
            agent=agent,
            agent_profile=agent_profile,
            repo_id=repo_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
            workspace_root=workspace_root,
            pma_enabled=pma_enabled,
        )
        await service._store.clear_pending_compact_seed(channel_id=channel_id)
        mode_label = "PMA" if pma_enabled else "repo"
        text = format_discord_message(
            f"Resumed {mode_label} session for `{service._format_agent_state(agent, agent_profile)}` with thread `{thread_id}`."
        )
    else:
        thread_items = service._list_discord_thread_targets_for_picker(
            workspace_root=workspace_root,
            agent=agent,
            agent_profile=agent_profile,
            current_thread_id=current_thread_id,
            mode=mode,
            repo_id=repo_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
        )
        if thread_items:
            header = (
                f"Current thread: `{current_thread_id}`\n\n"
                if current_thread_id
                else ""
            )
            await send_runtime_components_ephemeral(
                service,
                interaction_id,
                interaction_token,
                format_discord_message(
                    header + "Choose one thread to resume from the picker below:"
                ),
                [build_session_threads_picker(thread_items)],
            )
            return
        if current_thread_id:
            text = format_discord_message(
                f"Current thread: `{current_thread_id}`\n\n"
                "No additional threads found. Use `/car session resume thread_id:<thread_id>` to resume a specific thread."
            )
        else:
            text = format_discord_message(
                "No thread is currently active.\n\n"
                "Use `/car session resume thread_id:<thread_id>` to resume a specific thread, "
                "or start a new conversation to begin."
            )

    await service.send_or_respond_ephemeral(
        interaction_id=interaction_id,
        interaction_token=interaction_token,
        deferred=deferred,
        text=text,
    )


async def handle_car_reset(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
) -> None:
    from ..service import log_event

    deferred = await ensure_ephemeral_response_deferred(
        service,
        interaction_id,
        interaction_token,
    )

    binding = await service._store.get_binding(channel_id=channel_id)
    if binding is None:
        await service.send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text="Channel not bound. Use /car bind first.",
        )
        return

    pma_enabled = bool(binding.get("pma_enabled", False))
    workspace_raw = binding.get("workspace_path")
    workspace_root: Optional[Path] = None
    if isinstance(workspace_raw, str) and workspace_raw.strip():
        workspace_root = canonicalize_path(Path(workspace_raw))
        if not workspace_root.exists() or not workspace_root.is_dir():
            workspace_root = None

    if workspace_root is None:
        if pma_enabled:
            workspace_root = canonicalize_path(Path(service._config.root))
        else:
            await service.send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text="Binding is invalid. Run `/car bind path:<workspace>`.",
            )
            return

    agent, agent_profile = service._resolve_agent_state(binding)
    resource_kind = (
        str(binding.get("resource_kind")).strip()
        if isinstance(binding.get("resource_kind"), str)
        and str(binding.get("resource_kind")).strip()
        else None
    )
    resource_id = (
        str(binding.get("resource_id")).strip()
        if isinstance(binding.get("resource_id"), str)
        and str(binding.get("resource_id")).strip()
        else None
    )

    try:
        had_previous, _new_thread_id = await service._reset_discord_thread_binding(
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
            resource_kind=resource_kind,
            resource_id=resource_id,
            pma_enabled=pma_enabled,
        )
    except (
        RuntimeError,
        OSError,
        ValueError,
        TypeError,
    ) as exc:
        log_event(
            service._logger,
            logging.WARNING,
            "discord.reset.failed",
            channel_id=channel_id,
            workspace_root=str(workspace_root),
            agent=agent,
            agent_profile=agent_profile,
            exc=exc,
        )
        await service.send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text="Failed to reset Discord thread state.",
        )
        return
    await service._store.clear_pending_compact_seed(channel_id=channel_id)
    mode_label = "PMA" if pma_enabled else "repo"
    state_label = "cleared previous thread" if had_previous else "fresh state"

    await service.send_or_respond_ephemeral(
        interaction_id=interaction_id,
        interaction_token=interaction_token,
        deferred=deferred,
        text=(
            f"Reset {mode_label} thread state ({state_label}) for "
            f"`{service._format_agent_state(agent, agent_profile)}`."
        ),
    )


async def handle_car_archive(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
) -> None:
    from ....core.archive import (
        archive_workspace_for_fresh_start,
        resolve_workspace_archive_target,
    )
    from ..service import log_event

    workspace_root = await service._require_bound_workspace(
        interaction_id,
        interaction_token,
        channel_id=channel_id,
    )
    if workspace_root is None:
        return

    deferred = await ensure_ephemeral_response_deferred(
        service,
        interaction_id,
        interaction_token,
    )

    try:
        target = resolve_workspace_archive_target(
            workspace_root,
            hub_root=service._config.root,
            manifest_path=service._manifest_path,
        )
        result = await asyncio.to_thread(
            archive_workspace_for_fresh_start,
            hub_root=service._config.root,
            base_repo_root=target.base_repo_root,
            base_repo_id=target.base_repo_id,
            worktree_repo_root=workspace_root,
            worktree_repo_id=target.workspace_repo_id,
            branch=None,
            worktree_of=target.worktree_of,
            note="Discord /car archive",
            source_path=target.source_path,
        )
    except ValueError as exc:
        await service.send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=str(exc),
        )
        return
    except (RuntimeError, OSError) as exc:
        log_event(
            service._logger,
            logging.WARNING,
            "discord.archive_state.failed",
            workspace_root=str(workspace_root),
            exc=exc,
        )
        await service.send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=format_discord_message("Archive failed; check logs for details."),
        )
        return

    binding = await service._store.get_binding(channel_id=channel_id)
    if binding is not None:
        pma_enabled = bool(binding.get("pma_enabled", False))
        agent, agent_profile = service._resolve_agent_state(binding)
        try:
            await service._reset_discord_thread_binding(
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
            await service._store.clear_pending_compact_seed(channel_id=channel_id)
        except (RuntimeError, OSError, ValueError, TypeError) as exc:
            log_event(
                service._logger,
                logging.WARNING,
                "discord.archive_state.refresh_binding_failed",
                channel_id=channel_id,
                workspace_root=str(workspace_root),
                exc=exc,
            )
            await service.send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=format_discord_message(
                    "Archive completed, but preparing a fresh managed thread failed."
                ),
            )
            return

    await service.send_or_respond_ephemeral(
        interaction_id=interaction_id,
        interaction_token=interaction_token,
        deferred=deferred,
        text=format_discord_message(
            "\n".join(
                [
                    (
                        f"Archived workspace state to snapshot `{result.snapshot_id}`."
                        if result.snapshot_id
                        else "Workspace CAR state was already clean."
                    ),
                    f"Archived paths: {', '.join(result.archived_paths) or 'none'}",
                    (
                        f"Archived {len(result.archived_thread_ids)} managed thread"
                        f"{'' if len(result.archived_thread_ids) == 1 else 's'}."
                    ),
                    "The binding remains active for fresh work.",
                ]
            )
        ),
    )


def _interrupt_resolution_note(
    *,
    referenced_execution_id: Optional[str],
    running_execution: Any,
    resolved_execution: Any,
    thread_missing: bool,
) -> str:
    if thread_missing:
        return (
            "Status: this progress message no longer maps to an active managed thread."
        )
    running_execution_id = (
        str(getattr(running_execution, "execution_id", "") or "").strip() or None
    )
    if (
        referenced_execution_id
        and running_execution_id
        and running_execution_id != referenced_execution_id
    ):
        return "Status: this progress message belongs to an older turn. A newer turn is active."
    resolved_status = (
        str(getattr(resolved_execution, "status", "") or "").strip().lower()
    )
    if resolved_status == "ok":
        return "Status: this turn already completed."
    if resolved_status == "interrupted":
        return "Status: this turn was already stopped."
    if resolved_status == "error":
        return "Status: this turn already failed."
    if resolved_status == "queued":
        return "Status: this turn is queued and no longer has an active cancel surface."
    return "Status: this turn is no longer active."


async def _retire_stale_progress_message(
    service: Any,
    *,
    channel_id: str,
    message_id: Optional[str],
    note: str,
) -> None:
    from ..errors import DiscordAPIError

    normalized_message_id = str(message_id or "").strip()
    if not normalized_message_id:
        return
    content = note
    try:
        fetched = await service._rest.get_channel_message(
            channel_id=channel_id,
            message_id=normalized_message_id,
        )
    except (DiscordAPIError, RuntimeError, ConnectionError, OSError, ValueError):
        fetched = {}
    existing_content = str(fetched.get("content") or "").strip()
    if existing_content:
        lowered_existing = existing_content.lower()
        lowered_note = note.lower()
        if lowered_note not in lowered_existing:
            content = f"{existing_content.rstrip()}\n\n{note}"
        else:
            content = existing_content
    try:
        await service._rest.edit_channel_message(
            channel_id=channel_id,
            message_id=normalized_message_id,
            payload={
                "content": truncate_for_discord(
                    content,
                    max_len=max(int(service._config.max_message_length), 32),
                ),
                "components": [],
            },
        )
    except (DiscordAPIError, RuntimeError, ConnectionError, OSError, ValueError):
        return


async def handle_car_interrupt(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    active_turn_text: str = "Stopping current turn...",
    cancel_queued: bool = True,
    allow_promoted_no_active_success: bool = False,
    thread_target_id: Optional[str] = None,
    execution_id: Optional[str] = None,
    progress_reuse_source_message_id: Optional[str] = None,
    progress_reuse_acknowledgement: Optional[str] = None,
    source: str = "unknown",
    source_custom_id: Optional[str] = None,
    source_message_id: Optional[str] = None,
    source_command: Optional[str] = None,
    source_user_id: Optional[str] = None,
) -> None:
    from ..service import log_event

    log_event(
        service._logger,
        logging.INFO,
        "discord.interrupt.requested",
        channel_id=channel_id,
        interaction_id=interaction_id,
        source=source,
        source_user_id=source_user_id,
        source_command=source_command,
        source_custom_id=source_custom_id,
        source_message_id=source_message_id,
        requested_thread_target_id=thread_target_id,
        requested_execution_id=execution_id,
    )
    binding = await service._store.get_binding(channel_id=channel_id)
    normalized_thread_target_id = str(thread_target_id or "").strip() or None
    normalized_execution_id = str(execution_id or "").strip() or None
    if binding is None:
        if normalized_thread_target_id is None:
            text = format_discord_message(
                "This channel is not bound. Run `/car bind path:<workspace>` first."
            )
            await service.respond_ephemeral(interaction_id, interaction_token, text)
            return
        pma_enabled = False
        workspace_raw = None
    else:
        pma_enabled = bool(binding.get("pma_enabled", False))
        workspace_raw = binding.get("workspace_path")
    workspace_root: Optional[Path] = None
    if isinstance(workspace_raw, str) and workspace_raw.strip():
        candidate = canonicalize_path(Path(workspace_raw))
        if candidate.exists() and candidate.is_dir():
            workspace_root = candidate

    if workspace_root is None and pma_enabled:
        fallback = canonicalize_path(Path(service._config.root))
        if fallback.exists() and fallback.is_dir():
            workspace_root = fallback

    if workspace_root is None and binding is not None:
        text = format_discord_message(
            "Binding is invalid. Run `/car bind path:<workspace>` first."
        )
        await service.respond_ephemeral(interaction_id, interaction_token, text)
        return

    mode = "pma" if pma_enabled else "repo"
    if normalized_thread_target_id is not None:
        orchestration_service = service._discord_thread_service()
        _binding_row = None
        current_thread = orchestration_service.get_thread_target(
            normalized_thread_target_id
        )
    else:
        orchestration_service, _binding_row, current_thread = (
            service._get_discord_thread_binding(channel_id=channel_id, mode=mode)
        )
        normalized_thread_target_id = (
            str(getattr(current_thread, "thread_target_id", "") or "").strip() or None
        )
    if current_thread is None:
        if progress_reuse_source_message_id or progress_reuse_acknowledgement:
            clear_discord_turn_progress_reuse(
                service,
                thread_target_id=(
                    normalized_thread_target_id
                    or getattr(_binding_row, "thread_target_id", None)
                    or ""
                ),
            )
        if normalized_thread_target_id is not None:
            note = _interrupt_resolution_note(
                referenced_execution_id=normalized_execution_id,
                running_execution=None,
                resolved_execution=None,
                thread_missing=True,
            )
            await clear_discord_turn_progress_leases(
                service,
                managed_thread_id=normalized_thread_target_id,
                execution_id=normalized_execution_id,
            )
            await _retire_stale_progress_message(
                service,
                channel_id=channel_id,
                message_id=source_message_id,
                note=note,
            )
        log_event(
            service._logger,
            logging.INFO,
            "discord.interrupt.no_active_turn",
            channel_id=channel_id,
            interaction_id=interaction_id,
            source=source,
            source_user_id=source_user_id,
            source_command=source_command,
            source_custom_id=source_custom_id,
            source_message_id=source_message_id,
        )
        text = format_discord_message("No active turn to interrupt.")
        await service.respond_ephemeral(interaction_id, interaction_token, text)
        return
    deferred = await ensure_ephemeral_response_deferred(
        service,
        interaction_id,
        interaction_token,
    )
    get_running_execution = getattr(
        orchestration_service, "get_running_execution", None
    )
    running_execution = (
        get_running_execution(normalized_thread_target_id)
        if callable(get_running_execution) and normalized_thread_target_id is not None
        else None
    )
    if (
        normalized_execution_id is not None
        and running_execution is not None
        and str(getattr(running_execution, "execution_id", "") or "").strip()
        != normalized_execution_id
    ):
        note = _interrupt_resolution_note(
            referenced_execution_id=normalized_execution_id,
            running_execution=running_execution,
            resolved_execution=running_execution,
            thread_missing=False,
        )
        clear_discord_turn_progress_reuse(
            service,
            thread_target_id=normalized_thread_target_id or "",
        )
        await clear_discord_turn_progress_leases(
            service,
            managed_thread_id=normalized_thread_target_id,
            execution_id=normalized_execution_id,
        )
        await _retire_stale_progress_message(
            service,
            channel_id=channel_id,
            message_id=source_message_id,
            note=note,
        )
        text = format_discord_message("This progress message belongs to an older turn.")
        await service.send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=text,
        )
        return
    try:
        operation_store = None
        operation_store_getter = getattr(service, "_chat_operation_store_or_none", None)
        if callable(operation_store_getter):
            operation_store = operation_store_getter()
        interrupt_outcome = await request_managed_thread_interrupt(
            orchestration_service=orchestration_service,
            thread_target_id=current_thread.thread_target_id,
            cancel_queued=cancel_queued,
            referenced_execution_id=normalized_execution_id,
            operation_store=operation_store,
            operation_id=interaction_id,
        )
        interrupted_active = interrupt_outcome.interrupted_active
        recovered_lost_backend = interrupt_outcome.recovered_lost_backend
        cancelled_queued = interrupt_outcome.cancelled_queued
        log_event(
            service._logger,
            logging.INFO,
            "discord.interrupt.completed",
            channel_id=channel_id,
            interaction_id=interaction_id,
            source=source,
            source_user_id=source_user_id,
            source_command=source_command,
            source_custom_id=source_custom_id,
            source_message_id=source_message_id,
            thread_target_id=current_thread.thread_target_id,
            interrupt_state=interrupt_outcome.state.value,
            interrupted_active=interrupted_active,
            recovered_lost_backend=recovered_lost_backend,
            cancelled_queued=cancelled_queued,
            execution_id=interrupt_outcome.execution_id,
        )
        if interrupt_outcome.state == SharedInterruptState.FAILED_TO_DISPATCH:
            if progress_reuse_source_message_id or progress_reuse_acknowledgement:
                clear_discord_turn_progress_reuse(
                    service,
                    thread_target_id=current_thread.thread_target_id,
                )
            text = format_discord_message("Interrupt failed. Please try again.")
            await service.send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=text,
            )
            return
        if interrupt_outcome.state == SharedInterruptState.STILL_STOPPING:
            text = format_discord_message("Still stopping current turn...")
            await service.send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=text,
            )
            return
        if (
            not interrupted_active
            and not recovered_lost_backend
            and not cancelled_queued
        ):
            if allow_promoted_no_active_success:
                text = format_discord_message("Queued request moved to the front.")
                await service.send_or_respond_ephemeral(
                    interaction_id=interaction_id,
                    interaction_token=interaction_token,
                    deferred=deferred,
                    text=text,
                )
                return
            get_execution = getattr(orchestration_service, "get_execution", None)
            get_latest_execution = getattr(
                orchestration_service, "get_latest_execution", None
            )
            resolved_execution = None
            if callable(get_execution) and normalized_execution_id is not None:
                resolved_execution = get_execution(
                    current_thread.thread_target_id,
                    normalized_execution_id,
                )
            elif callable(get_latest_execution):
                resolved_execution = get_latest_execution(
                    current_thread.thread_target_id
                )
            if progress_reuse_source_message_id or progress_reuse_acknowledgement:
                clear_discord_turn_progress_reuse(
                    service,
                    thread_target_id=current_thread.thread_target_id,
                )
            note = _interrupt_resolution_note(
                referenced_execution_id=normalized_execution_id,
                running_execution=running_execution,
                resolved_execution=resolved_execution,
                thread_missing=False,
            )
            await clear_discord_turn_progress_leases(
                service,
                managed_thread_id=current_thread.thread_target_id,
                execution_id=normalized_execution_id,
            )
            await _retire_stale_progress_message(
                service,
                channel_id=channel_id,
                message_id=source_message_id,
                note=note,
            )
            text = format_discord_message("No active turn to interrupt.")
            await service.send_or_respond_ephemeral(
                interaction_id=interaction_id,
                interaction_token=interaction_token,
                deferred=deferred,
                text=(
                    format_discord_message("Current turn already finished.")
                    if interrupt_outcome.state == SharedInterruptState.ALREADY_FINISHED
                    else text
                ),
            )
            return
        if interrupted_active:
            request_discord_turn_progress_reuse(
                service,
                thread_target_id=current_thread.thread_target_id,
                source_message_id=str(progress_reuse_source_message_id or ""),
                acknowledgement=str(progress_reuse_acknowledgement or ""),
            )
        elif progress_reuse_source_message_id or progress_reuse_acknowledgement:
            clear_discord_turn_progress_reuse(
                service,
                thread_target_id=current_thread.thread_target_id,
            )
        elif source_message_id:
            await clear_discord_turn_progress_leases(
                service,
                managed_thread_id=current_thread.thread_target_id,
                execution_id=normalized_execution_id,
            )
        if recovered_lost_backend:
            await _retire_stale_progress_message(
                service,
                channel_id=channel_id,
                message_id=source_message_id,
                note="Status: this turn is no longer live in the backend and was recovered locally.",
            )
        text = format_discord_message(
            render_managed_thread_interrupt_message(
                interrupt_outcome,
                active_turn_text=active_turn_text,
                still_stopping_text="Still stopping current turn...",
                already_finished_text="Current turn already finished.",
                recovered_lost_backend_text=(
                    "Recovered stale session after backend thread was lost."
                ),
                queued_text_template="Cancelled {count} queued turn(s).",
            )
        )
        await service.send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=text,
        )

    except (RuntimeError, ConnectionError, OSError, ValueError) as exc:
        if progress_reuse_source_message_id or progress_reuse_acknowledgement:
            clear_discord_turn_progress_reuse(
                service,
                thread_target_id=current_thread.thread_target_id,
            )
        log_event(
            service._logger,
            logging.WARNING,
            "discord.interrupt.failed",
            channel_id=channel_id,
            interaction_id=interaction_id,
            source=source,
            source_user_id=source_user_id,
            source_command=source_command,
            source_custom_id=source_custom_id,
            source_message_id=source_message_id,
            workspace_root=str(workspace_root),
            thread_target_id=current_thread.thread_target_id,
            exc=exc,
        )
        text = format_discord_message("Interrupt failed. Please try again.")
        await service.send_or_respond_ephemeral(
            interaction_id=interaction_id,
            interaction_token=interaction_token,
            deferred=deferred,
            text=text,
        )


# Keep explicit module-level references so dead-code heuristics treat the
# Discord session command handlers as part of the intended surface.
_DISCORD_SESSION_COMMAND_HANDLERS = (
    handle_car_new,
    handle_car_newt,
    handle_car_newt_hard_reset,
    handle_car_newt_cancel,
    handle_car_resume,
    handle_car_reset,
    handle_car_archive,
)
