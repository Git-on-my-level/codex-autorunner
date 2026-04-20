from __future__ import annotations

from typing import Any

from codex_autorunner.integrations.chat.help_catalog import build_discord_help_lines
from codex_autorunner.integrations.telegram.handlers.commands_spec import (
    build_command_specs,
)
from codex_autorunner.integrations.telegram.helpers import _format_help_text


class _HandlerStub:
    def __getattr__(self, _name: str) -> Any:
        async def _noop(*_args: Any, **_kwargs: Any) -> None:
            return None

        return _noop


def test_telegram_help_lists_shared_commands_without_discord_only_entries() -> None:
    specs = build_command_specs(_HandlerStub())

    text = _format_help_text(specs)

    assert "/debug - Show debug info for troubleshooting" in text
    assert "/files - List or manage file inbox/outbox" in text
    assert "/files clear inbox|outbox|all" in text
    assert "/files send <filename>" in text
    assert "/pma - PMA mode controls (on/off/status)" in text
    assert "/tickets [search] - Browse tickets" in text
    assert "/contextspace - Browse contextspace docs" in text
    assert "/mcp -" not in text
    assert "/experimental -" not in text
    assert "/reply <message> (legacy)" in text


def test_discord_help_lists_session_file_flow_and_admin_sections() -> None:
    text = "\n".join(build_discord_help_lines())

    assert "/car tickets [search] - Browse tickets" in text
    assert "/car contextspace - Browse contextspace docs" in text
    assert "/car admin help - Show this help" in text
    assert "/car admin ids - Show chat/user IDs for debugging" in text
    assert "/car admin mcp" not in text
    assert "/car admin experimental" not in text
    assert "/car session resume [thread_id] - Resume a previous chat thread" in text
    assert "/car files inbox - List files in inbox" in text
    assert "/car files clear [target] - Clear inbox/outbox" in text
    assert "/flow runs [limit]" in text
    assert "/pma on - Enable PMA mode" in text
