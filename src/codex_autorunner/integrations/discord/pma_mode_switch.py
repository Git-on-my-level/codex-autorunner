from __future__ import annotations

from pathlib import Path
from typing import Any, Optional, cast

from ..chat.session_messages import (
    build_fresh_session_started_lines,
    build_thread_detail_lines,
)
from .rendering import format_discord_message


def _previous_binding_fields(
    binding: Optional[dict[str, Any]],
) -> tuple[Optional[str], Optional[str], Optional[str], Optional[str]]:
    if binding is None:
        return None, None, None, None
    return (
        binding.get("workspace_path"),
        binding.get("repo_id"),
        binding.get("resource_kind"),
        binding.get("resource_id"),
    )


def _normalized_optional_text(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _binding_thread_owner(
    binding: dict[str, Any],
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    return (
        _normalized_optional_text(binding.get("repo_id")),
        _normalized_optional_text(binding.get("resource_kind")),
        _normalized_optional_text(binding.get("resource_id")),
    )


def _render_fresh_mode_status(
    service: Any,
    binding: dict[str, Any],
    *,
    workspace_root: Path,
    thread_id: str,
    pma_enabled: bool,
    had_previous: bool,
    prefix_lines: tuple[str, ...] = (),
) -> str:
    agent, agent_profile = service._resolve_agent_state(binding)
    actor_label = service._format_agent_state(agent, agent_profile)
    mode_label = "PMA" if pma_enabled else "repo"
    state_label = "cleared previous thread" if had_previous else "new thread ready"
    lines = [
        *prefix_lines,
        *build_fresh_session_started_lines(
            mode_label=mode_label,
            actor_label=actor_label,
            state_label=state_label,
        ),
        *build_thread_detail_lines(
            thread_id=thread_id,
            workspace_path=str(workspace_root),
            actor_label=actor_label,
            model=service._status_model_label(binding),
            effort=service._status_effort_label(binding, agent),
        ),
    ]
    return format_discord_message("\n".join(lines))


async def _clear_seed_and_respond(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    text: str,
) -> None:
    await service._store.clear_pending_compact_seed(channel_id=channel_id)
    await service.respond_ephemeral(interaction_id, interaction_token, text)


async def _reset_thread_for_mode(
    service: Any,
    *,
    channel_id: str,
    binding: dict[str, Any],
    workspace_root: Path,
    pma_enabled: bool,
) -> tuple[bool, str]:
    agent, agent_profile = service._resolve_agent_state(binding)
    repo_id, resource_kind, resource_id = _binding_thread_owner(binding)
    return cast(
        tuple[bool, str],
        await service._reset_discord_thread_binding(
            channel_id=channel_id,
            workspace_root=workspace_root,
            agent=agent,
            agent_profile=agent_profile,
            repo_id=repo_id,
            resource_kind=resource_kind,
            resource_id=resource_id,
            pma_enabled=pma_enabled,
        ),
    )


async def _rollback_pma_on_attempt(
    service: Any,
    *,
    channel_id: str,
    had_binding_before: bool,
) -> None:
    """Revert PMA-on state when thread reset fails so the user can retry."""
    if had_binding_before:
        await service._store.update_pma_state(
            channel_id=channel_id,
            pma_enabled=False,
            pma_prev_workspace_path=None,
            pma_prev_repo_id=None,
            pma_prev_resource_kind=None,
            pma_prev_resource_id=None,
        )
    else:
        await service._store.delete_binding(channel_id=channel_id)


async def handle_pma_on_switch(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    guild_id: Optional[str],
) -> None:
    binding = await service._store.get_binding(channel_id=channel_id)
    if binding is not None and binding.get("pma_enabled", False):
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "PMA mode is already enabled for this channel. Use /pma off to exit.",
        )
        return
    had_binding_before = binding is not None
    prev_workspace, prev_repo_id, prev_resource_kind, prev_resource_id = (
        _previous_binding_fields(binding)
    )
    if binding is None:
        await service._store.upsert_binding(
            channel_id=channel_id,
            guild_id=guild_id,
            workspace_path=str(service._config.root),
            repo_id=None,
            resource_kind=None,
            resource_id=None,
        )
    await service._store.update_pma_state(
        channel_id=channel_id,
        pma_enabled=True,
        pma_prev_workspace_path=prev_workspace,
        pma_prev_repo_id=prev_repo_id,
        pma_prev_resource_kind=prev_resource_kind,
        pma_prev_resource_id=prev_resource_id,
    )
    binding = await service._store.get_binding(channel_id=channel_id)
    if binding is None:
        await _rollback_pma_on_attempt(
            service,
            channel_id=channel_id,
            had_binding_before=had_binding_before,
        )
        await _clear_seed_and_respond(
            service,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            text="PMA mode enabled, but the channel binding could not be loaded.",
        )
        return
    workspace_root = Path(service._config.root).resolve()
    try:
        had_previous, thread_id = await _reset_thread_for_mode(
            service,
            channel_id=channel_id,
            binding=binding,
            workspace_root=workspace_root,
            pma_enabled=True,
        )
    except (RuntimeError, OSError, ValueError, TypeError):
        await _rollback_pma_on_attempt(
            service,
            channel_id=channel_id,
            had_binding_before=had_binding_before,
        )
        await _clear_seed_and_respond(
            service,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            text="PMA mode enabled, but starting a fresh PMA session failed.",
        )
        return
    prefix_lines = (
        ("PMA mode enabled. Previous binding saved.",)
        if prev_workspace
        else ("PMA mode enabled.",)
    )
    await _clear_seed_and_respond(
        service,
        interaction_id,
        interaction_token,
        channel_id=channel_id,
        text=_render_fresh_mode_status(
            service,
            binding,
            workspace_root=workspace_root,
            thread_id=thread_id,
            pma_enabled=True,
            had_previous=had_previous,
            prefix_lines=prefix_lines,
        ),
    )


async def _restore_previous_binding(
    service: Any,
    *,
    channel_id: str,
    binding: dict[str, Any],
    prev_workspace: str,
    prev_repo_id: Optional[str],
    prev_resource_kind: Optional[str],
    prev_resource_id: Optional[str],
) -> Optional[dict[str, Any]]:
    await service._store.upsert_binding(
        channel_id=channel_id,
        guild_id=binding.get("guild_id"),
        workspace_path=prev_workspace,
        repo_id=prev_repo_id,
        resource_kind=prev_resource_kind,
        resource_id=prev_resource_id,
    )
    restored = await service._store.get_binding(channel_id=channel_id)
    if restored is None:
        return None
    workspace_root = Path(prev_workspace).resolve()
    if workspace_root.exists() and workspace_root.is_dir():
        return cast(dict[str, Any], restored)
    await service._store.delete_binding(channel_id=channel_id)
    return None


async def handle_pma_off_switch(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
) -> None:
    binding = await service._store.get_binding(channel_id=channel_id)
    if binding is None:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "PMA mode disabled. Back to repo mode.",
        )
        return
    prev_workspace = _normalized_optional_text(binding.get("pma_prev_workspace_path"))
    await service._store.update_pma_state(
        channel_id=channel_id,
        pma_enabled=False,
        pma_prev_workspace_path=None,
        pma_prev_repo_id=None,
        pma_prev_resource_kind=None,
        pma_prev_resource_id=None,
    )
    if prev_workspace is None:
        await service._store.delete_binding(channel_id=channel_id)
        await _clear_seed_and_respond(
            service,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            text=(
                "PMA mode disabled. No saved workspace binding was available, "
                "so this channel is now unbound."
            ),
        )
        return
    restored_binding = await _restore_previous_binding(
        service,
        channel_id=channel_id,
        binding=binding,
        prev_workspace=prev_workspace,
        prev_repo_id=_normalized_optional_text(binding.get("pma_prev_repo_id")),
        prev_resource_kind=_normalized_optional_text(
            binding.get("pma_prev_resource_kind")
        ),
        prev_resource_id=_normalized_optional_text(binding.get("pma_prev_resource_id")),
    )
    if restored_binding is None:
        await _clear_seed_and_respond(
            service,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            text=(
                "PMA mode disabled. The saved workspace is no longer available, "
                "so this channel is now unbound."
            ),
        )
        return
    workspace_root = Path(prev_workspace).resolve()
    try:
        had_previous, thread_id = await _reset_thread_for_mode(
            service,
            channel_id=channel_id,
            binding=restored_binding,
            workspace_root=workspace_root,
            pma_enabled=False,
        )
    except (RuntimeError, OSError, ValueError, TypeError):
        await _clear_seed_and_respond(
            service,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            text="PMA mode disabled, but starting a fresh repo session failed.",
        )
        return
    await _clear_seed_and_respond(
        service,
        interaction_id,
        interaction_token,
        channel_id=channel_id,
        text=_render_fresh_mode_status(
            service,
            restored_binding,
            workspace_root=workspace_root,
            thread_id=thread_id,
            pma_enabled=False,
            had_previous=had_previous,
            prefix_lines=("PMA mode disabled.",),
        ),
    )


__all__ = ["handle_pma_off_switch", "handle_pma_on_switch"]
