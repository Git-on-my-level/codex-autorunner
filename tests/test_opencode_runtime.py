import pytest

from codex_autorunner.agents.opencode.events import SSEEvent
from codex_autorunner.agents.opencode.runtime import (
    collect_opencode_output_from_events,
    parse_message_response,
)


async def _iter_events(events):
    for event in events:
        yield event


@pytest.mark.anyio
async def test_collect_output_uses_delta() -> None:
    events = [
        SSEEvent(
            event="message.part.updated",
            data='{"sessionID":"s1","properties":{"delta":{"text":"Hello "},'
            '"part":{"type":"text","text":"Hello "}}}',
        ),
        SSEEvent(
            event="message.part.updated",
            data='{"sessionID":"s1","properties":{"delta":{"text":"world"},'
            '"part":{"type":"text","text":"Hello world"}}}',
        ),
        SSEEvent(event="session.idle", data='{"sessionID":"s1"}'),
    ]
    output = await collect_opencode_output_from_events(
        _iter_events(events),
        session_id="s1",
    )
    assert output.text == "Hello world"
    assert output.error is None


@pytest.mark.anyio
async def test_collect_output_full_text_growth() -> None:
    events = [
        SSEEvent(
            event="message.part.updated",
            data='{"sessionID":"s1","properties":{"part":{"id":"p1","type":"text",'
            '"text":"Hello"}}}',
        ),
        SSEEvent(
            event="message.part.updated",
            data='{"sessionID":"s1","properties":{"part":{"id":"p1","type":"text",'
            '"text":"Hello world"}}}',
        ),
        SSEEvent(event="session.idle", data='{"sessionID":"s1"}'),
    ]
    output = await collect_opencode_output_from_events(
        _iter_events(events),
        session_id="s1",
    )
    assert output.text == "Hello world"
    assert output.error is None


@pytest.mark.anyio
async def test_collect_output_session_error() -> None:
    events = [
        SSEEvent(
            event="session.error",
            data='{"sessionID":"s1","error":{"message":"boom"}}',
        ),
        SSEEvent(event="session.idle", data='{"sessionID":"s1"}'),
    ]
    output = await collect_opencode_output_from_events(
        _iter_events(events),
        session_id="s1",
    )
    assert output.text == ""
    assert output.error == "boom"


def test_parse_message_response() -> None:
    payload = {
        "info": {"id": "turn-1", "error": "bad auth"},
        "parts": [{"type": "text", "text": "Hello"}],
    }
    result = parse_message_response(payload)
    assert result.text == "Hello"
    assert result.error == "bad auth"
