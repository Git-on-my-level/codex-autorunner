from __future__ import annotations

from codex_autorunner.integrations.chat.command_contract import COMMAND_CONTRACT


def test_command_contract_has_unique_ids_and_paths() -> None:
    ids = [entry.id for entry in COMMAND_CONTRACT]
    paths = [entry.path for entry in COMMAND_CONTRACT]

    assert len(ids) == len(set(ids))
    assert len(paths) == len(set(paths))


def test_command_contract_contains_expected_commands() -> None:
    manifest = {
        entry.id: (
            entry.path,
            entry.requires_bound_workspace,
            entry.status,
        )
        for entry in COMMAND_CONTRACT
    }

    assert manifest == {
        "car.agent": (("car", "agent"), True, "stable"),
        "car.model": (("car", "model"), True, "stable"),
        "car.status": (("car", "status"), False, "stable"),
        "car.new": (("car", "new"), True, "stable"),
        "car.update": (("car", "update"), False, "stable"),
        "pma.on": (("pma", "on"), False, "stable"),
        "pma.off": (("pma", "off"), False, "stable"),
        "pma.status": (("pma", "status"), False, "stable"),
    }
