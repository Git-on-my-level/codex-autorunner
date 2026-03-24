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
    assert "/pma - PMA mode controls (on/off/status)" in text
    assert "/tickets" not in text


def test_discord_help_lists_session_file_and_flow_sections() -> None:
    text = "\n".join(build_discord_help_lines())

    assert "/car tickets [search] - Browse and edit tickets" in text
    assert "/car session resume [thread_id] - Resume a previous chat thread" in text
    assert "/car files - List or manage file inbox/outbox" in text
    assert "/car flow runs [limit]" in text
    assert "/pma on - Enable PMA mode" in text
