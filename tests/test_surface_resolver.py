from __future__ import annotations

from typing import Optional

import pytest

from codex_autorunner.adapters.chat.surface_resolver import (
    DiscordSurfaceResolver,
    SurfaceInfo,
    resolve_surface_bindings,
)


class _FakeResolver:
    def __init__(self, *, prefix: str) -> None:
        self.prefix = prefix
        self.calls: list[str] = []

    async def resolve(self, key: str) -> Optional[SurfaceInfo]:
        self.calls.append(key)
        return SurfaceInfo(
            channel_id=key,
            name=f"{self.prefix}-{key}",
            surface_type="text",
            raw={"key": key},
        )


@pytest.mark.asyncio
async def test_surface_resolver_dispatches_by_surface_and_deduplicates() -> None:
    discord = _FakeResolver(prefix="discord")
    telegram = _FakeResolver(prefix="telegram")

    resolved = await resolve_surface_bindings(
        [
            {"surface_kind": "discord", "surface_key": "123"},
            {"surface_kind": "telegram", "surface_key": "-100:root"},
            {"surface_kind": "discord", "surface_key": "123"},
            {"surface_kind": "unknown", "surface_key": "ignored"},
        ],
        {"discord": discord, "telegram": telegram},
    )

    assert discord.calls == ["123"]
    assert telegram.calls == ["-100:root"]
    assert resolved[("discord", "123")].name == "discord-123"
    assert resolved[("telegram", "-100:root")].name == "telegram--100:root"
    assert resolved[("unknown", "ignored")] is None


@pytest.mark.asyncio
async def test_discord_resolver_ignores_non_channel_keys() -> None:
    resolver = DiscordSurfaceResolver(bot_token="token")

    assert await resolver.resolve("notification:notif-123") is None
