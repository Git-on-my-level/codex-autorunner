from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.agents.opencode.harness import OpenCodeHarness
from codex_autorunner.agents.registry import get_registered_agents
from codex_autorunner.core.sse import SSEEvent


class _StubClient:
    def __init__(self, events: list[SSEEvent]) -> None:
        self._events = events

    async def stream_events(self, *, directory: str | None = None):
        _ = directory
        for event in self._events:
            yield event


class _StubSupervisor:
    def __init__(self, client: _StubClient) -> None:
        self._client = client

    async def get_client(self, _workspace_root: Path) -> _StubClient:
        return self._client


@pytest.mark.asyncio
async def test_opencode_harness_reports_capabilities_from_contract() -> None:
    harness = OpenCodeHarness(_StubSupervisor(_StubClient([])))

    report = await harness.runtime_capability_report(Path("."))

    assert harness.capabilities == get_registered_agents()["opencode"].capabilities
    assert harness.supports("interrupt") is True
    assert harness.supports("review") is True
    assert harness.supports("approvals") is False
    assert report.capabilities == harness.capabilities


@pytest.mark.asyncio
async def test_opencode_harness_wait_for_turn_collects_plain_text_output() -> None:
    harness = OpenCodeHarness(
        _StubSupervisor(
            _StubClient(
                [
                    SSEEvent(
                        event="message.delta",
                        data='{"sessionID":"session-1","delta":"hello "}',
                    ),
                    SSEEvent(
                        event="message.completed",
                        data='{"sessionID":"session-1","text":"hello world"}',
                    ),
                    SSEEvent(
                        event="session.status",
                        data='{"sessionID":"session-1","properties":{"status":{"type":"idle"}}}',
                    ),
                ]
            )
        )
    )

    result = await harness.wait_for_turn(Path("."), "session-1", "turn-1")

    assert result.status == "ok"
    assert result.assistant_text == "hello world"
    assert result.errors == []


@pytest.mark.asyncio
async def test_opencode_harness_wait_for_turn_reports_errors() -> None:
    harness = OpenCodeHarness(
        _StubSupervisor(
            _StubClient(
                [
                    SSEEvent(
                        event="turn/error",
                        data='{"sessionID":"session-1","message":"stream failed"}',
                    ),
                    SSEEvent(
                        event="session.status",
                        data='{"sessionID":"session-1","properties":{"status":{"type":"idle"}}}',
                    ),
                ]
            )
        )
    )

    result = await harness.wait_for_turn(Path("."), "session-1", "turn-1")

    assert result.status == "error"
    assert result.assistant_text == ""
    assert result.errors == ["stream failed"]


@pytest.mark.asyncio
async def test_opencode_harness_wait_for_turn_collects_message_part_updates() -> None:
    harness = OpenCodeHarness(
        _StubSupervisor(
            _StubClient(
                [
                    SSEEvent(
                        event="message.part.updated",
                        data='{"sessionID":"session-1","properties":{"part":{"type":"text","text":"OK"}}}',
                    ),
                    SSEEvent(
                        event="session.status",
                        data='{"sessionID":"session-1","properties":{"status":{"type":"idle"}}}',
                    ),
                ]
            )
        )
    )

    result = await harness.wait_for_turn(Path("."), "session-1", "turn-1")

    assert result.status == "ok"
    assert result.assistant_text == "OK"
    assert result.errors == []


@pytest.mark.asyncio
async def test_opencode_harness_wait_for_turn_merges_cumulative_message_part_updates() -> (
    None
):
    harness = OpenCodeHarness(
        _StubSupervisor(
            _StubClient(
                [
                    SSEEvent(
                        event="message.part.updated",
                        data='{"sessionID":"session-1","properties":{"part":{"type":"text","text":"Hello"}}}',
                    ),
                    SSEEvent(
                        event="message.part.updated",
                        data='{"sessionID":"session-1","properties":{"part":{"type":"text","text":"Hello world"}}}',
                    ),
                    SSEEvent(
                        event="session.status",
                        data='{"sessionID":"session-1","properties":{"status":{"type":"idle"}}}',
                    ),
                ]
            )
        )
    )

    result = await harness.wait_for_turn(Path("."), "session-1", "turn-1")

    assert result.status == "ok"
    assert result.assistant_text == "Hello world"
    assert result.errors == []
