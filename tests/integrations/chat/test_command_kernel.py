from __future__ import annotations

from codex_autorunner.integrations.chat.command_kernel import (
    discord_command_kernel_entry,
    telegram_command_kernel_entry,
)


def test_bind_command_semantics_match_across_telegram_and_discord() -> None:
    telegram = telegram_command_kernel_entry("bind")
    discord = discord_command_kernel_entry(("car", "bind"))

    assert telegram is not None
    assert discord is not None
    assert discord.contract_id in telegram.contract_ids
    assert discord.canonical_path in telegram.canonical_paths
    assert discord.requires_bound_workspace is telegram.all_require_bound_workspace


def test_flow_command_kernel_groups_multiple_contract_paths() -> None:
    telegram = telegram_command_kernel_entry("flow")

    assert telegram is not None
    assert len(telegram.contract_ids) > 1
    assert ("car", "flow", "status") in telegram.canonical_paths
    assert ("car", "flow", "reply") in telegram.canonical_paths
