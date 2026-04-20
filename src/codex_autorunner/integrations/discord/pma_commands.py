from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from ..chat.status_diagnostics import build_process_monitor_lines_for_root
from .pma_mode_switch import handle_pma_off_switch, handle_pma_on_switch


async def handle_pma_command(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    guild_id: Optional[str],
    command_path: tuple[str, ...],
    options: Optional[dict[str, Any]] = None,
) -> None:
    if not service._config.pma_enabled:
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "PMA is disabled in hub config. Set pma.enabled: true to enable.",
        )
        return
    subcommand = command_path[1] if len(command_path) > 1 else "status"
    if subcommand == "on":
        await handle_pma_on(
            service,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
            guild_id=guild_id,
        )
        return
    if subcommand == "off":
        await handle_pma_off(
            service,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
        )
        return
    if subcommand == "status":
        await handle_pma_status(
            service,
            interaction_id,
            interaction_token,
            channel_id=channel_id,
        )
        return
    await service.respond_ephemeral(
        interaction_id,
        interaction_token,
        "Unknown PMA subcommand. Use on, off, or status.",
    )


async def handle_pma_command_from_normalized(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    guild_id: Optional[str],
    command_path: tuple[str, ...],
    options: dict[str, Any],
) -> None:
    _ = options
    subcommand = command_path[1] if len(command_path) > 1 else "status"
    if subcommand not in ("on", "off", "status"):
        await service.respond_ephemeral(
            interaction_id,
            interaction_token,
            "Unknown PMA subcommand. Use on, off, or status.",
        )
        return
    await handle_pma_command(
        service,
        interaction_id,
        interaction_token,
        channel_id=channel_id,
        guild_id=guild_id,
        command_path=command_path,
    )


async def handle_pma_on(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    guild_id: Optional[str],
) -> None:
    await handle_pma_on_switch(
        service,
        interaction_id,
        interaction_token,
        channel_id=channel_id,
        guild_id=guild_id,
    )


async def handle_pma_off(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
) -> None:
    await handle_pma_off_switch(
        service,
        interaction_id,
        interaction_token,
        channel_id=channel_id,
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
        root = getattr(getattr(service, "_config", None), "root", None)
        if root is not None:
            process_lines = build_process_monitor_lines_for_root(
                Path(root),
                include_history=False,
            )
            if process_lines:
                text = "\n".join([text, *process_lines])
    else:
        workspace = binding.get("workspace_path", "unknown")
        text = f"PMA mode: disabled\nCurrent workspace: {workspace}"
    await service.respond_ephemeral(interaction_id, interaction_token, text)
