from __future__ import annotations

from typing import Any

from codex_autorunner.integrations.telegram.commands_registry import (
    TelegramCommandDiff,
    build_command_payloads,
    diff_command_lists,
)
from codex_autorunner.integrations.telegram.handlers.commands_spec import CommandSpec


async def _dummy_handler(message: Any, args: str, handlers: Any) -> None:
    pass


def _spec(name: str, description: str = "", *, exposed: bool = True) -> CommandSpec:
    return CommandSpec(
        name=name,
        description=description,
        handler=_dummy_handler,
        exposed=exposed,
    )


class TestBuildCommandPayloads:
    def test_builds_valid_commands(self) -> None:
        specs = {
            "help": _spec("help", "Show help"),
            "status": _spec("status", "Show status"),
        }
        commands, invalid = build_command_payloads(specs)
        assert len(commands) == 2
        assert invalid == []
        names = {c["command"] for c in commands}
        assert names == {"help", "status"}

    def test_skips_unexposed_commands(self) -> None:
        specs = {
            "hidden": _spec("hidden", "Hidden", exposed=False),
        }
        commands, invalid = build_command_payloads(specs)
        assert commands == []
        assert invalid == []

    def test_rejects_invalid_command_names(self) -> None:
        specs = {
            "bad name": _spec("bad name", "desc"),
        }
        commands, invalid = build_command_payloads(specs)
        assert commands == []
        assert "bad name" in invalid

    def test_normalizes_to_lowercase(self) -> None:
        specs = {
            "Help": _spec("Help", "Show help"),
        }
        commands, invalid = build_command_payloads(specs)
        assert len(commands) == 1
        assert commands[0]["command"] == "help"

    def test_uses_name_as_fallback_description(self) -> None:
        specs = {
            "status": _spec("status", ""),
        }
        commands, _invalid = build_command_payloads(specs)
        assert commands[0]["description"] == "status"


class TestDiffCommandLists:
    def test_detects_additions(self) -> None:
        desired = [
            {"command": "help", "description": "Help"},
            {"command": "new", "description": "New"},
        ]
        current = [{"command": "help", "description": "Help"}]
        diff = diff_command_lists(desired, current)
        assert diff.added == ["new"]
        assert not diff.removed
        assert not diff.changed
        assert not diff.needs_update or diff.needs_update

    def test_detects_removals(self) -> None:
        desired = [{"command": "help", "description": "Help"}]
        current = [
            {"command": "help", "description": "Help"},
            {"command": "old", "description": "Old"},
        ]
        diff = diff_command_lists(desired, current)
        assert diff.removed == ["old"]

    def test_detects_description_changes(self) -> None:
        desired = [{"command": "help", "description": "New help"}]
        current = [{"command": "help", "description": "Old help"}]
        diff = diff_command_lists(desired, current)
        assert diff.changed == ["help"]

    def test_detects_order_change(self) -> None:
        desired = [
            {"command": "a", "description": "A"},
            {"command": "b", "description": "B"},
        ]
        current = [
            {"command": "b", "description": "B"},
            {"command": "a", "description": "A"},
        ]
        diff = diff_command_lists(desired, current)
        assert diff.order_changed is True

    def test_no_changes(self) -> None:
        desired = [{"command": "help", "description": "Help"}]
        current = [{"command": "help", "description": "Help"}]
        diff = diff_command_lists(desired, current)
        assert not diff.needs_update

    def test_needs_update_true_when_additions(self) -> None:
        diff = TelegramCommandDiff(
            added=["new"],
            removed=[],
            changed=[],
            order_changed=False,
        )
        assert diff.needs_update is True

    def test_needs_update_true_when_removals(self) -> None:
        diff = TelegramCommandDiff(
            added=[],
            removed=["old"],
            changed=[],
            order_changed=False,
        )
        assert diff.needs_update is True

    def test_needs_update_true_when_changed(self) -> None:
        diff = TelegramCommandDiff(
            added=[],
            removed=[],
            changed=["cmd"],
            order_changed=False,
        )
        assert diff.needs_update is True

    def test_needs_update_true_when_order_changed(self) -> None:
        diff = TelegramCommandDiff(
            added=[],
            removed=[],
            changed=[],
            order_changed=True,
        )
        assert diff.needs_update is True

    def test_needs_update_false_when_empty(self) -> None:
        diff = TelegramCommandDiff(
            added=[],
            removed=[],
            changed=[],
            order_changed=False,
        )
        assert diff.needs_update is False
