from __future__ import annotations

import logging

import pytest

from codex_autorunner.integrations.discord.command_registry import sync_commands
from codex_autorunner.integrations.discord.commands import (
    SUB_COMMAND,
    SUB_COMMAND_GROUP,
    build_application_commands,
)
from codex_autorunner.integrations.discord.interaction_registry import (
    autocomplete_route_for,
    component_route_for_custom_id,
    modal_route_for_custom_id,
    normalize_discord_command_path,
    slash_command_route_for_path,
)


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


def _registered_paths() -> set[tuple[str, ...]]:
    paths: set[tuple[str, ...]] = set()
    for command in build_application_commands():
        root = command["name"]
        for option in command.get("options", []):
            if option.get("type") == SUB_COMMAND:
                paths.add((root, option["name"]))
                continue
            if option.get("type") != SUB_COMMAND_GROUP:
                continue
            for subcommand in option.get("options", []):
                if subcommand.get("type") == SUB_COMMAND:
                    paths.add((root, option["name"], subcommand["name"]))
    return paths


def test_registered_slash_payloads_resolve_to_runtime_routes() -> None:
    for raw_path in _registered_paths():
        normalized = normalize_discord_command_path(raw_path)
        route = slash_command_route_for_path(normalized)
        assert route is not None, raw_path
        assert route.registered_path == raw_path


def test_registry_matches_high_risk_component_and_modal_patterns() -> None:
    assert component_route_for_custom_id("bind_page:next") is not None
    assert component_route_for_custom_id("bind_select") is not None
    assert component_route_for_custom_id("flow_runs_select") is not None
    assert component_route_for_custom_id("approval:req-1:approve") is not None
    assert component_route_for_custom_id("queue_cancel:message-1") is not None
    assert component_route_for_custom_id("qcancel:turn-1") is not None
    assert component_route_for_custom_id("queue_interrupt_send:message-1") is not None
    assert component_route_for_custom_id("qis:turn-1:message-1") is not None
    assert component_route_for_custom_id("newt_hard_reset:workspace-token") is not None
    assert component_route_for_custom_id("review_commit_select") is not None
    assert component_route_for_custom_id("flow_action_select:reply") is not None
    assert component_route_for_custom_id("update_confirm:discord") is not None
    assert component_route_for_custom_id("update_cancel:discord") is not None
    assert component_route_for_custom_id("tickets_select") is not None
    assert component_route_for_custom_id("tickets_filter_select") is not None
    assert modal_route_for_custom_id("tickets_modal:abc123") is not None


def test_registry_matches_autocomplete_routes_for_registered_paths() -> None:
    bind_route = autocomplete_route_for(("car", "bind"), "workspace")
    assert bind_route is not None
    assert bind_route.id == "car.bind.workspace"

    tickets_route = autocomplete_route_for(("car", "tickets"), "search")
    assert tickets_route is not None
    assert tickets_route.id == "car.tickets.search"

    flow_route = autocomplete_route_for(("flow", "status"), "run_id")
    assert flow_route is not None
    assert flow_route.id == "car.flow.status.run_id"
