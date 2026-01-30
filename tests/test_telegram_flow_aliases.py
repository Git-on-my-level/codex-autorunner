from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from codex_autorunner.integrations.telegram.adapter import TelegramMessage
from codex_autorunner.integrations.telegram.handlers.commands.flows import FlowCommands


class _TopicStoreStub:
    def __init__(self, repo_root: Path) -> None:
        self._record = SimpleNamespace(workspace_path=str(repo_root))

    async def get_topic(self, _key: str) -> SimpleNamespace:
        return self._record


class _FlowStatusAliasHandler(FlowCommands):
    def __init__(self) -> None:
        self.seen: list[str] = []

    async def _handle_flow(self, _message: TelegramMessage, args: str) -> None:
        self.seen.append(args)


class _FlowReplyAliasHandler(FlowCommands):
    def __init__(self, repo_root: Path) -> None:
        self._store = _TopicStoreStub(repo_root)
        self.reply_args: list[str] = []

    async def _resolve_topic_key(self, _chat_id: int, _thread_id: int | None) -> str:
        return "topic"

    async def _handle_reply(self, _message: TelegramMessage, args: str) -> None:
        self.reply_args.append(args)

    async def _send_message(
        self,
        _chat_id: int,
        _text: str,
        *,
        thread_id: int | None = None,
        reply_to: int | None = None,
        reply_markup: dict[str, object] | None = None,
    ) -> None:
        _ = (thread_id, reply_to, reply_markup)


def _message(text: str) -> TelegramMessage:
    return TelegramMessage(
        update_id=1,
        message_id=10,
        chat_id=999,
        thread_id=123,
        from_user_id=1,
        text=text,
        date=None,
        is_topic_message=True,
    )


@pytest.mark.anyio
async def test_flow_status_alias_forwards_to_flow() -> None:
    handler = _FlowStatusAliasHandler()
    await handler._handle_flow_status(_message("/flow_status"), "run-123")
    assert handler.seen == ["status run-123"]


@pytest.mark.anyio
async def test_flow_archive_alias_forwards_to_flow() -> None:
    handler = _FlowStatusAliasHandler()
    await handler._handle_flow_archive_command(
        _message("/flow-archive"), "run-123 --force"
    )
    assert handler.seen == ["archive run-123 --force"]


@pytest.mark.anyio
async def test_flow_reply_alias_routes_to_flow_reply(tmp_path: Path) -> None:
    handler = _FlowReplyAliasHandler(tmp_path)
    await handler._handle_flow(_message("/flow reply hello"), "reply hello world")
    assert handler.reply_args == ["hello world"]
