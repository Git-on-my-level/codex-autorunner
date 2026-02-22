from __future__ import annotations

from codex_autorunner.integrations.telegram.commands_registry import (
    build_command_payloads,
    diff_command_lists,
)
from codex_autorunner.integrations.telegram.handlers.commands import CommandSpec

# Cross-cutting parse/registration contract cases live in
# tests/test_telegram_command_contract.py. Keep this module registry-specific.


async def _noop_handler(*_args, **_kwargs) -> None:
    return None


def test_build_command_payloads_normalizes_names() -> None:
    specs = {
        "Run": CommandSpec("Run", "Start a task", _noop_handler),
        "Status": CommandSpec("Status", " Show status ", _noop_handler),
    }
    commands, invalid = build_command_payloads(specs)
    assert invalid == []
    assert commands == [
        {"command": "run", "description": "Start a task"},
        {"command": "status", "description": "Show status"},
    ]


def test_build_command_payloads_rejects_invalid_names() -> None:
    specs = {
        "Invalid-Hyphen": CommandSpec("foo-bar", "bad", _noop_handler),
        "Normalized-Upper": CommandSpec("Review", "bad", _noop_handler),
        "Invalid-Long": CommandSpec("a" * 33, "bad", _noop_handler),
    }
    commands, invalid = build_command_payloads(specs)
    assert commands == [{"command": "review", "description": "bad"}]
    assert invalid == ["foo-bar", "a" * 33]


def test_diff_command_lists_detects_changes() -> None:
    desired = [
        {"command": "run", "description": "Start a task"},
        {"command": "status", "description": "Show status"},
    ]
    current = [
        {"command": "status", "description": "Show status"},
        {"command": "help", "description": "Help"},
    ]
    diff = diff_command_lists(desired, current)
    assert diff.added == ["run"]
    assert diff.removed == ["help"]
    assert diff.changed == []
    assert diff.needs_update is True


def test_diff_command_lists_detects_order_changes() -> None:
    desired = [
        {"command": "run", "description": "Start a task"},
        {"command": "status", "description": "Show status"},
    ]
    current = [
        {"command": "status", "description": "Show status"},
        {"command": "run", "description": "Start a task"},
    ]
    diff = diff_command_lists(desired, current)
    assert diff.added == []
    assert diff.removed == []
    assert diff.changed == []
    assert diff.order_changed is True
