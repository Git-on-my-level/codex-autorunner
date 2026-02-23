from __future__ import annotations

from codex_autorunner.integrations.telegram.commands_registry import (
    build_command_payloads,
    diff_command_lists,
)
from tests.fixtures.telegram_command_helpers import make_command_spec

# Cross-cutting parse/registration contract cases live in
# tests/test_telegram_command_contract.py. Keep this module registry-specific.
# Helper usage: prefer `make_command_spec(...)` for concise registry test setup.


def test_build_command_payloads_normalizes_names() -> None:
    specs = {
        "Run": make_command_spec("Run", "Start a task"),
        "Status": make_command_spec("Status", " Show status "),
    }
    commands, invalid = build_command_payloads(specs)
    assert invalid == []
    assert commands == [
        {"command": "run", "description": "Start a task"},
        {"command": "status", "description": "Show status"},
    ]


def test_build_command_payloads_rejects_invalid_names() -> None:
    specs = {
        "Invalid-Hyphen": make_command_spec("foo-bar", "bad"),
        "Invalid-Whitespace": make_command_spec("foo bar", "bad"),
        "Invalid-Mention": make_command_spec("foo@codexbot", "bad"),
        "Normalized-Upper": make_command_spec("Review", "bad"),
        "Invalid-Long": make_command_spec("a" * 33, "bad"),
    }
    commands, invalid = build_command_payloads(specs)
    assert commands == [{"command": "review", "description": "bad"}]
    assert invalid == ["foo-bar", "foo bar", "foo@codexbot", "a" * 33]


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
