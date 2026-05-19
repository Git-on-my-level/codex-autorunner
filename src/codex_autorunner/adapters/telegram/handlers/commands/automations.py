from __future__ import annotations

from pathlib import Path
from typing import Any

from .....adapters.chat.automation_surface import (
    USAGE,
    automation_status_for_chat,
    list_automations_for_chat,
    run_automation_for_chat,
    set_automation_enabled_for_chat,
)
from ...adapter import TelegramMessage


class AutomationCommands:
    async def _handle_automation(
        self, message: TelegramMessage, args: str, _runtime: Any
    ) -> None:
        hub_root = getattr(self, "_hub_root", None) or getattr(
            getattr(self, "_config", None), "root", None
        )
        if hub_root is None:
            await self._send_message(
                message.chat_id,
                "Automation state is unavailable because the hub root is not configured.",
                thread_id=message.thread_id,
                reply_to=message.message_id,
            )
            return

        parts = self._parse_command_args(args)
        action = (parts[0].lower() if parts else "list").strip()
        try:
            text = self._automation_command_text(Path(hub_root), action, parts[1:])
        except KeyError as exc:
            text = f"Automation not found: {exc.args[0]}"
        except ValueError as exc:
            text = str(exc)

        await self._send_message(
            message.chat_id,
            text,
            thread_id=message.thread_id,
            reply_to=message.message_id,
        )

    def _automation_command_text(
        self, hub_root: Path, action: str, args: list[str]
    ) -> str:
        if action in {"list", "ls"}:
            return list_automations_for_chat(hub_root)
        if action == "status":
            return automation_status_for_chat(hub_root, _first_arg(args))
        if action == "run":
            return run_automation_for_chat(
                hub_root,
                _first_arg(args),
                source="telegram",
                supervisor=getattr(self, "_hub_supervisor", None),
            )
        if action in {"pause", "disable"}:
            return set_automation_enabled_for_chat(
                hub_root,
                _first_arg(args),
                enabled=False,
            )
        if action in {"resume", "enable"}:
            return set_automation_enabled_for_chat(
                hub_root,
                _first_arg(args),
                enabled=True,
            )
        return USAGE


def _first_arg(args: list[str]) -> str:
    if not args:
        raise ValueError("automation id is required")
    return args[0]


__all__ = ["AutomationCommands"]
