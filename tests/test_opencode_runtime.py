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


@pytest.mark.anyio
async def test_collect_output_auto_replies_question() -> None:
    replies = []

    async def _reply(request_id: str, answers: list[list[str]]) -> None:
        replies.append((request_id, answers))

    events = [
        SSEEvent(
            event="question.asked",
            data='{"sessionID":"s1","properties":{"id":"q1","questions":[{"text":"Continue?",'
            '"options":[{"label":"Yes"},{"label":"No"}]}]}}',
        ),
        SSEEvent(event="session.idle", data='{"sessionID":"s1"}'),
    ]
    output = await collect_opencode_output_from_events(
        _iter_events(events),
        session_id="s1",
        question_policy="auto_first_option",
        reply_question=_reply,
    )
    assert output.text == ""
    assert replies == [("q1", [["Yes"]])]


@pytest.mark.anyio
async def test_collect_output_question_deduplicates() -> None:
    replies = []

    async def _reply(request_id: str, answers: list[list[str]]) -> None:
        replies.append((request_id, answers))

    events = [
        SSEEvent(
            event="question.asked",
            data='{"sessionID":"s1","properties":{"id":"q1","questions":[{"text":"Continue?",'
            '"options":[{"label":"Yes"},{"label":"No"}]}]}}',
        ),
        SSEEvent(
            event="question.asked",
            data='{"sessionID":"s1","properties":{"id":"q1","questions":[{"text":"Continue?",'
            '"options":[{"label":"Yes"},{"label":"No"}]}]}}',
        ),
        SSEEvent(event="session.idle", data='{"sessionID":"s1"}'),
    ]
    await collect_opencode_output_from_events(
        _iter_events(events),
        session_id="s1",
        question_policy="auto_first_option",
        reply_question=_reply,
    )
    assert len(replies) == 1


@pytest.mark.anyio
async def test_collect_output_filters_reasoning_and_includes_legacy_none_type() -> None:
    events = [
        # Legacy text part with type=None should be included in output
        SSEEvent(
            event="message.part.updated",
            data='{"sessionID":"s1","properties":{"delta":{"text":"Hello "},"part":{"text":"Hello "}}}',
        ),
        # Explicit text part should be included in output
        SSEEvent(
            event="message.part.updated",
            data='{"sessionID":"s1","properties":{"delta":{"text":"world"},"part":{"type":"text","text":"world"}}}',
        ),
        # Reasoning part should be excluded from output
        SSEEvent(
            event="message.part.updated",
            data='{"sessionID":"s1","properties":{"delta":{"text":"thinking..."},"part":{"type":"reasoning","id":"r1","text":"thinking..."}}}',
        ),
        # Another text part with type=None should be included
        SSEEvent(
            event="message.part.updated",
            data='{"sessionID":"s1","properties":{"delta":{"text":"!"},"part":{"text":"!"}}}',
        ),
        SSEEvent(event="session.idle", data='{"sessionID":"s1"}'),
    ]
    output = await collect_opencode_output_from_events(
        _iter_events(events),
        session_id="s1",
    )
    # All text content (except reasoning) should be in output
    # This tests that parts with type=None (legacy) are included
    assert output.text == "Hello world!"
    # Reasoning should be excluded
    assert "thinking" not in output.text.lower()
    assert output.error is None


def test_parse_message_response() -> None:
    payload = {
        "info": {"id": "turn-1", "error": "bad auth"},
        "parts": [{"type": "text", "text": "Hello"}],
    }
    result = parse_message_response(payload)
    assert result.text == "Hello"
    assert result.error == "bad auth"
