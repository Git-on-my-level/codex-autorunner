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


class _FlowReplyAliasHandler(FlowCommands):
    def __init__(self, repo_root: Path, *, explode: bool = False) -> None:
        self._store = _TopicStoreStub(repo_root)
        self.reply_args: list[str] = []
        self.explode = explode

    async def _resolve_topic_key(self, _chat_id: int, _thread_id: int | None) -> str:
        return "topic"

    async def _handle_reply(self, _message: TelegramMessage, args: str) -> None:
        if self.explode:
            raise RuntimeError("boom")
        self.reply_args.append(args)

    def _resolve_workspace(self, _key: str) -> tuple[str, Path] | None:
        return None

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


class _FlowStartRestartAliasHandler(FlowCommands):
    def __init__(self, repo_root: Path) -> None:
        self._store = _TopicStoreStub(repo_root)
        self.bootstrap_calls: list[list[str]] = []
        self.restart_calls: list[list[str]] = []

    async def _resolve_topic_key(self, _chat_id: int, _thread_id: int | None) -> str:
        return "topic"

    async def _handle_flow_bootstrap(
        self, _message: TelegramMessage, _repo_root: Path, argv: list[str]
    ) -> None:
        self.bootstrap_calls.append(argv)

    async def _handle_flow_restart(
        self, _message: TelegramMessage, _repo_root: Path, argv: list[str]
    ) -> None:
        self.restart_calls.append(argv)

    def _resolve_workspace(self, _key: str) -> tuple[str, Path] | None:
        return None

    async def _send_message(
        self,
        _chat_id: int,
        _text: str,
        *,
        thread_id: int | None = None,
        reply_to: int | None = None,
        reply_markup: dict[str, object] | None = None,
        parse_mode: str | None = None,
    ) -> None:
        _ = (thread_id, reply_to, reply_markup, parse_mode)


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
async def test_flow_reply_alias_routes_to_flow_reply(tmp_path: Path) -> None:
    handler = _FlowReplyAliasHandler(tmp_path)
    await handler._handle_flow(_message("/flow reply hello"), "reply hello world")
    assert handler.reply_args == ["hello world"]


@pytest.mark.anyio
async def test_flow_command_errors_are_reported(tmp_path: Path) -> None:
    handler = _FlowReplyAliasHandler(tmp_path, explode=True)
    message = _message("/flow reply hello")
    await handler._handle_flow(message, "reply hello world")

    assert handler.reply_args == []


@pytest.mark.anyio
async def test_flow_start_alias_routes_to_bootstrap(tmp_path: Path) -> None:
    handler = _FlowStartRestartAliasHandler(tmp_path)
    await handler._handle_flow(_message("/flow start --force-new"), "start --force-new")
    assert handler.bootstrap_calls == [["--force-new"]]
    assert handler.restart_calls == []


@pytest.mark.anyio
async def test_flow_restart_alias_routes_to_restart(tmp_path: Path) -> None:
    handler = _FlowStartRestartAliasHandler(tmp_path)
    await handler._handle_flow(
        _message("/flow restart abc"), "restart 01234567-89ab-cdef-0123-456789abcdef"
    )
    assert handler.restart_calls == [["01234567-89ab-cdef-0123-456789abcdef"]]
    assert handler.bootstrap_calls == []
