from __future__ import annotations

from typing import Optional

import pytest

from codex_autorunner.integrations.telegram.adapter import TelegramMessage
from codex_autorunner.integrations.telegram.handlers.commands_runtime import (
    TelegramCommandHandlers,
    _RuntimeStub,
)
from codex_autorunner.integrations.telegram.state import TelegramTopicRecord


class _RateLimitClientStub:
    async def request(
        self,
        _method: str,
        *,
        params: Optional[dict[str, object]] = None,
        timeout: float,
    ) -> dict[str, object]:
        _ = (params, timeout)
        return {
            "rateLimits": {
                "primary": {"used_percent": 4, "window_minutes": 300},
            }
        }


class _RouterStub:
    def __init__(self, record: TelegramTopicRecord, runtime: _RuntimeStub) -> None:
        self._record = record
        self._runtime = runtime

    async def ensure_topic(
        self, _chat_id: int, _thread_id: Optional[int]
    ) -> TelegramTopicRecord:
        return self._record

    def runtime_for(self, _key: str) -> _RuntimeStub:
        return self._runtime


class _StoreStub:
    async def pending_approvals_for_key(self, _key: str) -> list[object]:
        return []


class _StatusHandlerStub(TelegramCommandHandlers):
    def __init__(
        self,
        record: TelegramTopicRecord,
        runtime: _RuntimeStub,
        *,
        client: Optional[_RateLimitClientStub] = None,
    ) -> None:
        self._router = _RouterStub(record, runtime)
        self._store = _StoreStub()
        self._client = client
        self._client_calls = 0
        self._sent_messages: list[str] = []
        self._token_usage_by_thread: dict[str, dict[str, object]] = {}
        self._manifest_path = None
        self._hub_root = None

    async def _resolve_topic_key(self, chat_id: int, thread_id: Optional[int]) -> str:
        return f"{chat_id}:{thread_id}"

    async def _refresh_workspace_id(
        self, _key: str, _record: TelegramTopicRecord
    ) -> None:
        return None

    def _effective_policies(self, _record: TelegramTopicRecord) -> tuple[None, None]:
        return None, None

    async def _send_message(
        self,
        _chat_id: int,
        text: str,
        *,
        thread_id: Optional[int] = None,
        reply_to: Optional[int] = None,
        reply_markup: Optional[dict[str, object]] = None,
    ) -> None:
        _ = (thread_id, reply_to, reply_markup)
        self._sent_messages.append(text)

    async def _client_for_workspace(
        self, _workspace_path: Optional[str]
    ) -> _RateLimitClientStub:
        self._client_calls += 1
        if self._client is None:
            raise AssertionError("client should not be requested")
        return self._client


def _message() -> TelegramMessage:
    return TelegramMessage(
        update_id=1,
        message_id=1,
        chat_id=10,
        thread_id=12,
        from_user_id=2,
        text="/status",
        date=None,
        is_topic_message=True,
    )


@pytest.mark.anyio
async def test_status_opencode_skips_rate_limits() -> None:
    record = TelegramTopicRecord(workspace_path="/tmp", agent="opencode")
    runtime = _RuntimeStub()
    handler = _StatusHandlerStub(record, runtime)

    await handler._handle_status(_message(), runtime=runtime)

    assert handler._client_calls == 0
    assert "Limits:" not in handler._sent_messages[-1]


@pytest.mark.anyio
async def test_status_codex_includes_rate_limits() -> None:
    record = TelegramTopicRecord(workspace_path="/tmp", agent="codex")
    runtime = _RuntimeStub()
    handler = _StatusHandlerStub(record, runtime, client=_RateLimitClientStub())

    await handler._handle_status(_message(), runtime=runtime)

    assert handler._client_calls == 1
    assert "Limits:" in handler._sent_messages[-1]


@pytest.mark.anyio
async def test_status_pma_mode_skips_bind_hint() -> None:
    record = TelegramTopicRecord(pma_enabled=True)
    runtime = _RuntimeStub()
    handler = _StatusHandlerStub(record, runtime, client=_RateLimitClientStub())

    await handler._handle_status(_message(), runtime=runtime)

    assert handler._sent_messages
    text = handler._sent_messages[-1]
    assert "PMA" in text
    assert "Use /bind" not in text
