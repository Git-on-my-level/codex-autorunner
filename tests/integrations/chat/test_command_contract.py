from __future__ import annotations

from typing import Any

from codex_autorunner.integrations.chat.command_contract import COMMAND_CONTRACT
from codex_autorunner.integrations.discord.commands import (
    SUB_COMMAND,
    SUB_COMMAND_GROUP,
    build_application_commands,
)
from codex_autorunner.integrations.telegram.handlers.commands_spec import (
    build_command_specs,
)


class _HandlerStub:
    def __getattr__(self, _name: str) -> Any:
        async def _noop(*_args: Any, **_kwargs: Any) -> None:
            return None

        return _noop


def _discord_registered_paths() -> set[tuple[str, ...]]:
    paths: set[tuple[str, ...]] = set()
    for command in build_application_commands():
        root = command.get("name")
        options = command.get("options")
        if not isinstance(root, str) or not isinstance(options, list):
            continue
        for option in options:
            if not isinstance(option, dict):
                continue
            option_name = option.get("name")
            option_type = option.get("type")
            if not isinstance(option_name, str):
                continue
            if option_type == SUB_COMMAND:
                paths.add((root, option_name))
                continue
            if option_type != SUB_COMMAND_GROUP:
                continue
            nested = option.get("options")
            if not isinstance(nested, list):
                continue
            for sub in nested:
                if not isinstance(sub, dict):
                    continue
                sub_name = sub.get("name")
                sub_type = sub.get("type")
                if isinstance(sub_name, str) and sub_type == SUB_COMMAND:
                    paths.add((root, option_name, sub_name))
    return paths


def _telegram_registered_commands() -> set[str]:
    specs = build_command_specs(_HandlerStub())
    return set(specs.keys())


def test_command_contract_has_unique_ids_and_paths() -> None:
    ids = [entry.id for entry in COMMAND_CONTRACT]
    paths = [entry.path for entry in COMMAND_CONTRACT]

    assert len(ids) == len(set(ids))
    assert len(paths) == len(set(paths))


def test_command_contract_catalogs_all_registered_surface_commands() -> None:
    contract_discord_paths = {
        path for entry in COMMAND_CONTRACT for path in entry.discord_paths
    }
    contract_telegram_commands = {
        name for entry in COMMAND_CONTRACT for name in entry.telegram_commands
    }

    assert contract_discord_paths == _discord_registered_paths()
    assert contract_telegram_commands == _telegram_registered_commands()


def test_command_contract_status_and_mapping_invariants() -> None:
    by_id = {entry.id: entry for entry in COMMAND_CONTRACT}

    assert {entry.status for entry in COMMAND_CONTRACT} <= {
        "stable",
        "partial",
        "unsupported",
    }

    stable_missing_surface = [
        entry.id
        for entry in COMMAND_CONTRACT
        if entry.status == "stable"
        and (not entry.telegram_commands or not entry.discord_paths)
    ]
    assert stable_missing_surface == []

    assert by_id["car.agent"].status == "stable"
    assert by_id["car.flow.status"].status == "partial"
    assert by_id["car.review"].status == "partial"
