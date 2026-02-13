import logging
from types import SimpleNamespace
from typing import Any

import pytest

from codex_autorunner.integrations.telegram.adapter import (
    TelegramCommand,
    TelegramMessage,
)
from codex_autorunner.integrations.telegram.handlers.commands import CommandSpec
from codex_autorunner.integrations.telegram.handlers.commands_runtime import (
    TelegramCommandHandlers,
)


class _AliasHarness(TelegramCommandHandlers):
    def __init__(self) -> None:
        self._logger = logging.getLogger(__name__)
        self._resume_options: dict[str, Any] = {}
        self._bind_options: dict[str, Any] = {}
        self._agent_options: dict[str, Any] = {}
        self._model_options: dict[str, Any] = {}
        self._model_pending: dict[str, Any] = {}
        self.sent_messages: list[str] = []
        self.model_calls: list[str] = []

        async def _handle_model_alias(
            _message: TelegramMessage, args: str, _runtime: Any
        ) -> None:
            self.model_calls.append(args)

        self._command_specs = {
            "model": CommandSpec(
                name="model",
                description="list or set the model",
                handler=_handle_model_alias,
            )
        }

    async def _resolve_topic_key(self, _chat_id: int, _thread_id: Any) -> str:
        return "1:root"

    async def _send_message(
        self,
        _chat_id: int,
        text: str,
        *,
        thread_id: Any = None,
        reply_to: Any = None,
    ) -> None:
        self.sent_messages.append(text)


def _message() -> TelegramMessage:
    return TelegramMessage(
        update_id=1,
        message_id=2,
        chat_id=3,
        thread_id=4,
        from_user_id=5,
        text="/models",
        date=0,
        is_topic_message=False,
    )


@pytest.mark.asyncio
async def test_models_alias_routes_to_model_handler() -> None:
    harness = _AliasHarness()
    runtime = SimpleNamespace(current_turn_id=None)
    command = TelegramCommand(name="models", args="list", raw="/models list")

    await harness._handle_command(command, _message(), runtime)

    assert harness.model_calls == ["list"]
    assert harness.sent_messages == []
