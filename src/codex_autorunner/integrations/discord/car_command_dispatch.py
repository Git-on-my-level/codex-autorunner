from __future__ import annotations

from typing import Any, Optional

from .interaction_registry import dispatch_slash_command


async def handle_car_command(
    service: Any,
    interaction_id: str,
    interaction_token: str,
    *,
    channel_id: str,
    guild_id: Optional[str],
    user_id: Optional[str],
    command_path: tuple[str, ...],
    options: dict[str, Any],
) -> None:
    await dispatch_slash_command(
        service,
        interaction_id,
        interaction_token,
        channel_id=channel_id,
        guild_id=guild_id,
        user_id=user_id,
        command_path=command_path,
        options=options,
    )
