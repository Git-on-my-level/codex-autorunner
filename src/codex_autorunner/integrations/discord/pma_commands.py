from __future__ import annotations

from typing import Any, Optional


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


async def handle_pma_on(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    guild_id: Optional[str],
) -> None:
    binding = await service._store.get_binding(channel_id=channel_id)
    if binding is not None and binding.get("pma_enabled", False):
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            "PMA mode is already enabled for this channel. Use /pma off to exit.",
        )
        return

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
    await service._store.clear_pending_compact_seed(channel_id=channel_id)

    await service._respond_ephemeral(
        interaction_id,
        interaction_token,
        (
            "PMA mode enabled. Use /pma off to exit. Previous binding saved."
            if prev_workspace
            else "PMA mode enabled. Use /pma off to exit."
        ),
    )


async def handle_pma_off(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
) -> None:
    binding = await service._store.get_binding(channel_id=channel_id)
    if binding is None:
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            "PMA mode disabled. Back to repo mode.",
        )
        return

    prev_workspace = binding.get("pma_prev_workspace_path")
    prev_repo_id = binding.get("pma_prev_repo_id")
    prev_resource_kind = binding.get("pma_prev_resource_kind")
    prev_resource_id = binding.get("pma_prev_resource_id")

    await service._store.update_pma_state(
        channel_id=channel_id,
        pma_enabled=False,
        pma_prev_workspace_path=None,
        pma_prev_repo_id=None,
        pma_prev_resource_kind=None,
        pma_prev_resource_id=None,
    )
    await service._store.clear_pending_compact_seed(channel_id=channel_id)

    if prev_workspace:
        await service._store.upsert_binding(
            channel_id=channel_id,
            guild_id=binding.get("guild_id"),
            workspace_path=prev_workspace,
            repo_id=prev_repo_id,
            resource_kind=prev_resource_kind,
            resource_id=prev_resource_id,
        )
        hint = f"Restored binding to {prev_workspace}."
    else:
        await service._store.delete_binding(channel_id=channel_id)
        hint = "Back to repo mode."

    await service._respond_ephemeral(
        interaction_id,
        interaction_token,
        f"PMA mode disabled. {hint}",
    )


async def handle_pma_status(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
) -> None:
    binding = await service._store.get_binding(channel_id=channel_id)
    if binding is None:
        text = "PMA mode: disabled\nCurrent workspace: unbound"
    elif binding.get("pma_enabled", False):
        text = "PMA mode: enabled"
    else:
        workspace = binding.get("workspace_path", "unknown")
        text = f"PMA mode: disabled\nCurrent workspace: {workspace}"
    await service._respond_ephemeral(interaction_id, interaction_token, text)
