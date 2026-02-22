from __future__ import annotations

import logging

import pytest

from codex_autorunner.integrations.discord.command_registry import sync_commands


class _FakeRest:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def bulk_overwrite_application_commands(
        self,
        *,
        application_id: str,
        commands: list[dict],
        guild_id: str | None = None,
    ) -> list[dict]:
        self.calls.append(
            {
                "application_id": application_id,
                "guild_id": guild_id,
                "commands": commands,
            }
        )
        return commands


@pytest.mark.anyio
async def test_sync_commands_global_scope_overwrites_once() -> None:
    rest = _FakeRest()
    commands = [{"name": "car"}]

    await sync_commands(
        rest,
        application_id="app-1",
        commands=commands,
        scope="global",
        guild_ids=(),
        logger=logging.getLogger("test"),
    )

    assert len(rest.calls) == 1
    assert rest.calls[0]["application_id"] == "app-1"
    assert rest.calls[0]["guild_id"] is None


@pytest.mark.anyio
async def test_sync_commands_guild_scope_overwrites_each_guild() -> None:
    rest = _FakeRest()
    commands = [{"name": "car"}]

    await sync_commands(
        rest,
        application_id="app-1",
        commands=commands,
        scope="guild",
        guild_ids=("guild-b", "guild-a", "guild-b"),
        logger=logging.getLogger("test"),
    )

    assert [call["guild_id"] for call in rest.calls] == ["guild-a", "guild-b"]


@pytest.mark.anyio
async def test_sync_commands_guild_scope_requires_guild_ids() -> None:
    rest = _FakeRest()

    with pytest.raises(ValueError):
        await sync_commands(
            rest,
            application_id="app-1",
            commands=[{"name": "car"}],
            scope="guild",
            guild_ids=(),
            logger=logging.getLogger("test"),
        )
