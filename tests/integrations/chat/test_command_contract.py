from __future__ import annotations

from typing import Any

from codex_autorunner.integrations.chat.command_contract import (
    COMMAND_CONTRACT,
    telegram_command_metadata_for_name,
    telegram_runtime_command_names_from_contract,
)
from codex_autorunner.integrations.discord.commands import (
    SUB_COMMAND,
    SUB_COMMAND_GROUP,
    build_application_commands,
)
from codex_autorunner.integrations.discord.service import DiscordBotService
from codex_autorunner.integrations.telegram.commands_registry import (
    build_command_payloads,
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
    commands, invalid = build_command_payloads(specs)
    assert invalid == []
    return {entry["command"] for entry in commands}


def _contract_entry_for_path(path: tuple[str, ...]) -> Any:
    for entry in COMMAND_CONTRACT:
        if entry.path == path or path in entry.discord_paths:
            return entry
    return None


def test_command_contract_has_unique_ids_and_paths() -> None:
    ids = [entry.id for entry in COMMAND_CONTRACT]
    paths = [entry.path for entry in COMMAND_CONTRACT]

    assert len(ids) == len(set(ids))
    assert len(paths) == len(set(paths))


def test_command_contract_discord_paths_are_unique() -> None:
    seen: dict[tuple[str, ...], str] = {}
    for entry in COMMAND_CONTRACT:
        for discord_path in entry.discord_paths:
            prev = seen.get(discord_path)
            assert prev is None, (
                f"duplicate discord_paths entry {discord_path!r}: "
                f"{prev} and {entry.id}"
            )
            seen[discord_path] = entry.id


def test_command_contract_entry_resolves_discord_path_aliases() -> None:
    """Discord ``/car session resume`` uses a longer path than logical ``car.resume``."""
    entry = _contract_entry_for_path(("car", "session", "resume"))
    assert entry is not None
    assert entry.id == "car.resume"
    assert entry.discord_ack_policy == "defer_ephemeral"


def test_command_contract_discord_registered_paths_resolve_for_dispatch() -> None:
    """Every registered slash path must map to a contract entry after Discord normalization.

    Otherwise ``_prepare_command_interaction`` skips defer and handlers rely on a
    second ACK attempt within the interaction deadline.
    """
    for raw_path in _discord_registered_paths():
        normalized = DiscordBotService._normalize_discord_command_path(raw_path)
        assert _contract_entry_for_path(normalized) is not None, (
            f"no COMMAND_CONTRACT entry for Discord path {raw_path!r} "
            f"(normalized {normalized!r})"
        )


def test_command_contract_catalogs_all_registered_surface_commands() -> None:
    contract_discord_paths = {
        path for entry in COMMAND_CONTRACT for path in entry.discord_paths
    }
    contract_telegram_commands = {
        name
        for name in telegram_runtime_command_names_from_contract()
        if telegram_command_metadata_for_name(name).exposure == "public"
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
    assert by_id["car.mcp"].status == "partial"


def test_command_contract_discord_metadata_is_present_for_registered_paths() -> None:
    for entry in COMMAND_CONTRACT:
        if not entry.discord_paths:
            assert entry.discord_ack_policy is None
            assert entry.discord_exposure is None
            continue
        assert entry.discord_ack_policy in {
            "immediate",
            "defer_ephemeral",
            "defer_public",
            "defer_component_update",
        }
        assert entry.discord_ack_timing in {"dispatch", "post_private_preflight"}
        assert entry.discord_exposure in {"public", "operator"}

    by_id = {entry.id: entry for entry in COMMAND_CONTRACT}
    assert by_id["car.flow.status"].discord_ack_timing == "dispatch"
    assert by_id["car.flow.runs"].discord_ack_timing == "dispatch"
    assert by_id["car.flow.start"].discord_ack_timing == "dispatch"
    assert by_id["car.flow.restart"].discord_ack_timing == "dispatch"


def test_command_contract_uses_canonical_runtime_capabilities_for_agent_linked_commands() -> (
    None
):
    by_id = {entry.id: entry for entry in COMMAND_CONTRACT}

    assert by_id["car.resume"].required_capabilities == ("durable_threads",)
    assert by_id["car.reset"].required_capabilities == ("durable_threads",)
    assert by_id["car.review"].required_capabilities == ("review",)
    assert by_id["car.compact"].required_capabilities == ("message_turns",)
    assert by_id["car.interrupt"].required_capabilities == ("interrupt",)


def test_command_contract_telegram_metadata_is_present_for_runtime_commands() -> None:
    for name in telegram_runtime_command_names_from_contract():
        metadata = telegram_command_metadata_for_name(name)
        assert metadata is not None
        assert metadata.exposure in {"public", "hidden", "legacy_alias"}
        assert metadata.response_policy == "typing"
        assert isinstance(metadata.allow_during_turn, bool)

    assert telegram_command_metadata_for_name("mcp").exposure == "hidden"
    assert telegram_command_metadata_for_name("experimental").exposure == "hidden"
    assert telegram_command_metadata_for_name("reply").exposure == "legacy_alias"
