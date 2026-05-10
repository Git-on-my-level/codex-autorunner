from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from codex_autorunner.adapters.app_server.client import (
    CodexAppServerResponseError,
)
from codex_autorunner.agents.codex.harness import CodexHarness
from codex_autorunner.agents.codex.harness import logger as codex_harness_logger
from codex_autorunner.agents.registry import get_registered_agents


class _TurnHandle:
    def __init__(self, result: object) -> None:
        self._result = result

    async def wait(self, *, timeout: float | None = None) -> object:
        _ = timeout
        return self._result


class _Supervisor:
    def __init__(self, client: object) -> None:
        self._client = client

    async def get_client(self, _workspace_root: Path) -> object:
        return self._client


class _EventsBuffer:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    async def list_events(
        self,
        thread_id: str,
        turn_id: str,
        *,
        after_id: int = 0,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append((thread_id, turn_id, after_id, limit))
        return [{"id": 1}]


@pytest.mark.asyncio
async def test_codex_harness_list_progress_events_delegates_to_events_buffer() -> None:
    events = _EventsBuffer()
    harness = CodexHarness(
        supervisor=_Supervisor(object()),
        events=events,  # type: ignore[arg-type]
    )
    got = await harness.list_progress_events("thread-a", "turn-b")
    assert got == [{"id": 1}]
    assert events.calls == [("thread-a", "turn-b", 0, None)]


@pytest.mark.asyncio
async def test_codex_harness_stream_events_warns_when_stream_entries_missing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    harness = CodexHarness(supervisor=_Supervisor(object()), events=object())  # type: ignore[arg-type]
    with caplog.at_level(logging.WARNING, logger=codex_harness_logger.name):
        events = [
            e async for e in harness.stream_events(Path("."), "thread-a", "turn-b")
        ]
    assert events == []
    assert any("stream_entries not callable" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_codex_harness_reports_capabilities_from_contract() -> None:
    harness = CodexHarness(supervisor=object(), events=object())  # type: ignore[arg-type]

    report = await harness.runtime_capability_report(Path("."))

    assert harness.capabilities == get_registered_agents()["codex"].capabilities
    assert harness.supports("review") is True
    assert harness.supports("interrupt") is True
    assert harness.supports("approvals") is True
    assert harness.supports("transcript_history") is False
    assert report.capabilities == harness.capabilities


@pytest.mark.asyncio
async def test_codex_harness_wait_for_turn_returns_plain_text_terminal_result() -> None:
    harness = CodexHarness(supervisor=object(), events=object())  # type: ignore[arg-type]
    harness._turn_handles[("thread-1", "turn-1")] = _TurnHandle(  # type: ignore[attr-defined]
        SimpleNamespace(
            status="completed",
            final_message="fallback message",
            agent_messages=["first line", "second line"],
            errors=[],
            raw_events=[{"method": "message.completed"}],
        )
    )

    result = await harness.wait_for_turn(Path("."), "thread-1", "turn-1")

    assert result.status == "completed"
    assert result.assistant_text == "fallback message"
    assert result.errors == []
    assert result.raw_events == [{"method": "message.completed"}]
    assert ("thread-1", "turn-1") not in harness._turn_handles  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_codex_harness_resume_conversation_ignores_missing_thread_failures() -> (
    None
):
    class _Client:
        async def thread_resume(self, _thread_id: str) -> None:
            raise CodexAppServerResponseError(
                method="thread/resume",
                code=-32000,
                message="Thread not found",
            )

    harness = CodexHarness(supervisor=_Supervisor(_Client()), events=object())  # type: ignore[arg-type]

    resumed = await harness.resume_conversation(Path("."), "thread-1")

    assert resumed.id == "thread-1"


@pytest.mark.asyncio
async def test_codex_harness_surfaces_native_thread_titles() -> None:
    class _Client:
        async def thread_list(self) -> dict[str, Any]:
            return {
                "threads": [
                    {
                        "id": "thread-1",
                        "name": "Compare chat title sources",
                        "summary": "summary",
                    }
                ]
            }

    harness = CodexHarness(supervisor=_Supervisor(_Client()), events=object())  # type: ignore[arg-type]

    conversations = await harness.list_conversations(Path("."))

    assert conversations[0].id == "thread-1"
    assert conversations[0].title == "Compare chat title sources"
    assert conversations[0].summary == "summary"


@pytest.mark.asyncio
async def test_codex_harness_sets_native_thread_title_after_create() -> None:
    class _Client:
        def __init__(self) -> None:
            self.name_set_calls: list[tuple[str, str]] = []

        async def thread_start(self, _workspace: str) -> dict[str, Any]:
            return {"id": "thread-1"}

        async def thread_name_set(self, thread_id: str, name: str) -> None:
            self.name_set_calls.append((thread_id, name))

    client = _Client()
    harness = CodexHarness(supervisor=_Supervisor(client), events=object())  # type: ignore[arg-type]

    conversation = await harness.new_conversation(
        Path("."),
        title="Compare chat title sources",
    )

    assert conversation.title == "Compare chat title sources"
    assert client.name_set_calls == [("thread-1", "Compare chat title sources")]


@pytest.mark.asyncio
async def test_codex_harness_resume_conversation_propagates_non_missing_failures() -> (
    None
):
    class _Client:
        async def thread_resume(self, _thread_id: str) -> None:
            raise CodexAppServerResponseError(
                method="thread/resume",
                code=-32000,
                message="permission denied",
            )

    harness = CodexHarness(supervisor=_Supervisor(_Client()), events=object())  # type: ignore[arg-type]

    with pytest.raises(CodexAppServerResponseError, match="permission denied"):
        await harness.resume_conversation(Path("."), "thread-1")
