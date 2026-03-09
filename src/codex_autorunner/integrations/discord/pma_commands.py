from __future__ import annotations

from typing import Any, Optional


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

    prev_workspace = binding.get("workspace_path") if binding is not None else None
    prev_repo_id = binding.get("repo_id") if binding is not None else None

    if binding is None:
        await service._store.upsert_binding(
            channel_id=channel_id,
            guild_id=guild_id,
            workspace_path=str(service._config.root),
            repo_id=None,
        )

    await service._store.update_pma_state(
        channel_id=channel_id,
        pma_enabled=True,
        pma_prev_workspace_path=prev_workspace,
        pma_prev_repo_id=prev_repo_id,
    )

    hint = (
        "Use /pma off to exit. Previous binding saved."
        if prev_workspace
        else "Use /pma off to exit."
    )
    await service._respond_ephemeral(
        interaction_id,
        interaction_token,
        f"PMA mode enabled. {hint}",
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

    await service._store.update_pma_state(
        channel_id=channel_id,
        pma_enabled=False,
        pma_prev_workspace_path=None,
        pma_prev_repo_id=None,
    )

    if prev_workspace:
        await service._store.upsert_binding(
            channel_id=channel_id,
            guild_id=binding.get("guild_id"),
            workspace_path=prev_workspace,
            repo_id=prev_repo_id,
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
        await service._respond_ephemeral(
            interaction_id,
            interaction_token,
            "\n".join(
                [
                    "PMA mode: disabled",
                    "Current workspace: unbound",
                ]
            ),
        )
        return

    pma_enabled = binding.get("pma_enabled", False)
    status = "enabled" if pma_enabled else "disabled"

    if pma_enabled:
        lines = [f"PMA mode: {status}"]
    else:
        workspace = binding.get("workspace_path", "unknown")
        lines = [
            f"PMA mode: {status}",
            f"Current workspace: {workspace}",
        ]

    await service._respond_ephemeral(
        interaction_id,
        interaction_token,
        "\n".join(lines),
    )
