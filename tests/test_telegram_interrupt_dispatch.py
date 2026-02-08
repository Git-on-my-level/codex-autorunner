from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest

from codex_autorunner.integrations.telegram.service import TelegramBotService


class _RouterStub:
    def __init__(self, records: dict[str, object]) -> None:
        self._records = records

    async def get_topic(self, key: str) -> Optional[object]:
        return self._records.get(key)


class _CodexClientStub:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Optional[str]]] = []

    async def turn_interrupt(
        self, turn_id: str, *, thread_id: Optional[str] = None
    ) -> None:
        self.calls.append((turn_id, thread_id))


class _OpenCodeClientStub:
    def __init__(self) -> None:
        self.abort_calls: list[str] = []

    async def abort(self, session_id: str) -> None:
        self.abort_calls.append(session_id)


class _OpenCodeSupervisorStub:
    def __init__(self, client: _OpenCodeClientStub) -> None:
        self._client = client
        self.roots: list[Path] = []

    async def get_client(self, root: Path) -> _OpenCodeClientStub:
        self.roots.append(root)
        return self._client


class _DispatchServiceStub:
    def __init__(
        self,
        *,
        records: dict[str, object],
        resolved_key: str,
        turn_ctx: object,
        codex_client: _CodexClientStub,
        opencode_supervisor: Optional[_OpenCodeSupervisorStub] = None,
    ) -> None:
        self._logger = logging.getLogger("test")
        self._router = _RouterStub(records)
        self._resolved_key = resolved_key
        self._turn_ctx = turn_ctx
        self._codex_client = codex_client
        self._opencode_supervisor = opencode_supervisor
        self.workspace_requests: list[Optional[str]] = []
        self.edits: list[tuple[int, int, str]] = []

    async def _resolve_topic_key(self, _chat_id: int, _thread_id: Optional[int]) -> str:
        return self._resolved_key

    def _resolve_turn_context(
        self, _turn_id: Optional[str], *, thread_id: Optional[str] = None
    ) -> object:
        _ = thread_id
        return self._turn_ctx

    async def _client_for_workspace(self, workspace_path: Optional[str]):
        self.workspace_requests.append(workspace_path)
        return self._codex_client

    async def _edit_message_text(self, chat_id: int, message_id: int, text: str) -> bool:
        self.edits.append((chat_id, message_id, text))
        return True


@pytest.mark.anyio
async def test_interrupt_uses_turn_context_topic_for_codex_workspace() -> None:
    codex_client = _CodexClientStub()
    records = {
        "current": SimpleNamespace(agent="codex", workspace_path=None, active_thread_id=None),
        "scoped": SimpleNamespace(
            agent="codex",
            workspace_path="/tmp/scoped-workspace",
            active_thread_id="thread-scoped",
        ),
    }
    service = _DispatchServiceStub(
        records=records,
        resolved_key="current",
        turn_ctx=SimpleNamespace(topic_key="scoped"),
        codex_client=codex_client,
    )
    runtime = SimpleNamespace(
        interrupt_requested=True,
        interrupt_message_id=77,
        interrupt_turn_id="turn-1",
    )

    await TelegramBotService._dispatch_interrupt_request(
        service,
        turn_id="turn-1",
        codex_thread_id="thread-scoped",
        runtime=runtime,
        chat_id=123,
        thread_id=456,
    )

    assert service.workspace_requests == ["/tmp/scoped-workspace"]
    assert codex_client.calls == [("turn-1", "thread-scoped")]
    assert service.edits == []


@pytest.mark.anyio
async def test_interrupt_uses_turn_context_topic_for_opencode_session() -> None:
    codex_client = _CodexClientStub()
    opencode_client = _OpenCodeClientStub()
    opencode_supervisor = _OpenCodeSupervisorStub(opencode_client)
    records = {
        "current": SimpleNamespace(agent="codex", workspace_path=None, active_thread_id=None),
        "scoped": SimpleNamespace(
            agent="opencode",
            workspace_path="/tmp/opencode-workspace",
            active_thread_id=None,
        ),
    }
    service = _DispatchServiceStub(
        records=records,
        resolved_key="current",
        turn_ctx=SimpleNamespace(topic_key="scoped"),
        codex_client=codex_client,
        opencode_supervisor=opencode_supervisor,
    )
    runtime = SimpleNamespace(
        interrupt_requested=True,
        interrupt_message_id=88,
        interrupt_turn_id="turn-2",
    )

    await TelegramBotService._dispatch_interrupt_request(
        service,
        turn_id="turn-2",
        codex_thread_id="session-from-turn",
        runtime=runtime,
        chat_id=123,
        thread_id=456,
    )

    assert service.workspace_requests == []
    assert opencode_supervisor.roots == [Path("/tmp/opencode-workspace")]
    assert opencode_client.abort_calls == ["session-from-turn"]
    assert service.edits == []

