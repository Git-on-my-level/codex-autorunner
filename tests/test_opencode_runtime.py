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
    seen_deltas: list[str] = []

    async def _part_handler(part_type: str, part: dict[str, str], delta_text):
        if part_type == "text" and delta_text:
            seen_deltas.append(delta_text)

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
        part_handler=_part_handler,
    )
    # Deltas are added to final output (for progress) and sent to part_handler
    assert output.text == "Hello world"
    assert seen_deltas == ["Hello ", "world"]
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


@pytest.mark.anyio
async def test_collect_output_skips_reasoning_when_type_missing_on_delta() -> None:
    events = [
        SSEEvent(
            event="message.part.updated",
            data='{"sessionID":"s1","properties":{"delta":{"text":"think"},"part":{"type":"reasoning","id":"r1","text":"think"}}}',
        ),
        SSEEvent(
            event="message.part.updated",
            data='{"sessionID":"s1","properties":{"delta":{"text":" more"},"part":{"id":"r1","text":"think more"}}}',
        ),
        SSEEvent(
            event="message.part.updated",
            data='{"sessionID":"s1","properties":{"delta":{"text":"Hello"},"part":{"type":"text","text":"Hello"}}}',
        ),
        SSEEvent(event="session.idle", data='{"sessionID":"s1"}'),
    ]
    output = await collect_opencode_output_from_events(
        _iter_events(events),
        session_id="s1",
    )
    assert output.text == "Hello"
    assert "think" not in output.text.lower()


@pytest.mark.anyio
async def test_collect_output_emits_usage_from_properties_info_tokens() -> None:
    seen: list[dict[str, int]] = []

    async def _part_handler(part_type: str, part: dict[str, int], delta_text):
        if part_type == "usage":
            seen.append(part)

    events = [
        SSEEvent(
            event="message.updated",
            data=(
                '{"sessionID":"s1","properties":{"info":{"tokens":'
                '{"input":10,"output":5,"reasoning":2,"cache":{"read":1}},'
                '"modelContextWindow":2000}}}'
            ),
        ),
        SSEEvent(event="session.idle", data='{"sessionID":"s1"}'),
    ]
    output = await collect_opencode_output_from_events(
        _iter_events(events),
        session_id="s1",
        part_handler=_part_handler,
    )
    assert output.text == ""
    assert len(seen) == 1
    usage = seen[0]
    assert usage["totalTokens"] == 18
    assert usage["inputTokens"] == 10
    assert usage["cachedInputTokens"] == 1
    assert usage["outputTokens"] == 5
    assert usage["reasoningTokens"] == 2
    assert usage["modelContextWindow"] == 2000


@pytest.mark.anyio
async def test_collect_output_backfills_context_from_providers() -> None:
    seen: list[dict[str, int]] = []

    async def _part_handler(part_type: str, part: dict[str, int], delta_text):
        if part_type == "usage":
            seen.append(part)

    events = [
        SSEEvent(
            event="message.updated",
            data='{"sessionID":"s1","info":{"tokens":{"input":12,"output":3}}}',
        ),
        SSEEvent(event="session.idle", data='{"sessionID":"s1"}'),
    ]

    async def _fetch_providers():
        return {
            "providers": [
                {"id": "prov", "models": {"model": {"limit": {"context": 1024}}}}
            ]
        }

    output = await collect_opencode_output_from_events(
        _iter_events(events),
        session_id="s1",
        model_payload={"providerID": "prov", "modelID": "model"},
        part_handler=_part_handler,
        provider_fetcher=_fetch_providers,
    )
    assert output.text == ""
    assert output.error is None
    assert seen == [
        {
            "providerID": "prov",
            "modelID": "model",
            "totalTokens": 15,
            "inputTokens": 12,
            "outputTokens": 3,
            "modelContextWindow": 1024,
        }
    ]


@pytest.mark.anyio
async def test_collect_output_skips_usage_for_non_primary_session() -> None:
    seen: list[dict[str, int]] = []

    async def _part_handler(part_type: str, part: dict[str, int], delta_text):
        if part_type == "usage":
            seen.append(part)

    events = [
        SSEEvent(
            event="message.updated",
            data=(
                '{"sessionID":"s2","properties":{"info":{"tokens":'
                '{"input":3,"output":4}}}}'
            ),
        ),
        SSEEvent(event="session.idle", data='{"sessionID":"s1"}'),
    ]
    output = await collect_opencode_output_from_events(
        _iter_events(events),
        session_id="s1",
        progress_session_ids={"s1", "s2"},
        part_handler=_part_handler,
    )
    assert output.text == ""
    assert seen == []


@pytest.mark.anyio
async def test_collect_output_uses_completed_text_when_no_parts() -> None:
    events = [
        SSEEvent(
            event="message.completed",
            data='{"sessionID":"s1","info":{"id":"m1","role":"assistant"},'
            '"parts":[{"type":"text","text":"Hello"}]}',
        ),
        SSEEvent(event="session.idle", data='{"sessionID":"s1"}'),
    ]
    output = await collect_opencode_output_from_events(
        _iter_events(events),
        session_id="s1",
    )
    assert output.text == "Hello"
    assert output.error is None


@pytest.mark.anyio
async def test_collect_output_ignores_completed_text_when_role_missing() -> None:
    events = [
        SSEEvent(
            event="message.completed",
            data='{"sessionID":"s1","info":{"id":"m1","role":null},'
            '"parts":[{"type":"text","text":"User prompt"}]}',
        ),
        SSEEvent(event="session.idle", data='{"sessionID":"s1"}'),
    ]
    output = await collect_opencode_output_from_events(
        _iter_events(events),
        session_id="s1",
    )
    assert output.text == ""
    assert output.error is None


@pytest.mark.anyio
async def test_collect_output_uses_completed_text_after_role_update() -> None:
    events = [
        SSEEvent(
            event="message.completed",
            data='{"sessionID":"s1","info":{"id":"m1","role":null},'
            '"parts":[{"type":"text","text":"Hello"}]}',
        ),
        SSEEvent(
            event="message.updated",
            data='{"sessionID":"s1","info":{"id":"m1","role":"assistant"}}',
        ),
        SSEEvent(event="session.idle", data='{"sessionID":"s1"}'),
    ]
    output = await collect_opencode_output_from_events(
        _iter_events(events),
        session_id="s1",
    )
    assert output.text == "Hello"
    assert output.error is None


@pytest.mark.anyio
async def test_collect_output_drops_user_completed_when_role_missing() -> None:
    events = [
        SSEEvent(
            event="message.completed",
            data='{"sessionID":"s1","info":{"id":"u1","role":null},'
            '"parts":[{"type":"text","text":"User prompt"}]}',
        ),
        SSEEvent(
            event="message.completed",
            data='{"sessionID":"s1","info":{"id":"a1","role":"assistant"},'
            '"parts":[{"type":"text","text":"Assistant response"}]}',
        ),
        SSEEvent(event="session.idle", data='{"sessionID":"s1"}'),
    ]
    output = await collect_opencode_output_from_events(
        _iter_events(events),
        session_id="s1",
    )
    assert output.text == "Assistant response"
    assert output.error is None


@pytest.mark.anyio
async def test_collect_output_dedupes_completed_before_part_updates() -> None:
    events = [
        SSEEvent(
            event="message.completed",
            data='{"sessionID":"s1","info":{"id":"m1","role":"assistant"},'
            '"parts":[{"type":"text","text":"Hello"}]}',
        ),
        SSEEvent(
            event="message.part.updated",
            data='{"sessionID":"s1","properties":{"delta":{"text":"Hello"},'
            '"part":{"id":"p1","messageId":"m1","type":"text","text":"Hello"}}}',
        ),
        SSEEvent(event="session.idle", data='{"sessionID":"s1"}'),
    ]
    output = await collect_opencode_output_from_events(
        _iter_events(events),
        session_id="s1",
    )
    assert output.text == "Hello"
    assert output.error is None


@pytest.mark.anyio
async def test_collect_output_does_not_duplicate_when_final_part_update_has_no_delta() -> (
    None
):
    seen_deltas: list[str] = []

    async def _part_handler(part_type: str, part: dict[str, str], delta_text):
        if part_type == "text" and delta_text:
            seen_deltas.append(delta_text)

    events = [
        # Delta updates for a text part
        SSEEvent(
            event="message.part.updated",
            data='{"sessionID":"s1","properties":{"delta":{"text":"Hello "},'
            '"part":{"id":"p1","type":"text","text":"Hello "}}}',
        ),
        SSEEvent(
            event="message.part.updated",
            data='{"sessionID":"s1","properties":{"delta":{"text":"world!"},'
            '"part":{"id":"p1","type":"text","text":"Hello world!"}}}',
        ),
        # Final part update with full text, no delta (with time.end)
        SSEEvent(
            event="message.part.updated",
            data='{"sessionID":"s1","properties":{"part":{"id":"p1","type":"text",'
            '"text":"Hello world!","time":{"end":"2024-01-01T00:00:00Z"}}}',
        ),
        SSEEvent(event="session.idle", data='{"sessionID":"s1"}'),
    ]
    output = await collect_opencode_output_from_events(
        _iter_events(events),
        session_id="s1",
        part_handler=_part_handler,
    )
    # Deltas are sent to part_handler
    assert seen_deltas == ["Hello ", "world!"]
    # Final output contains the text exactly once (from deltas), not duplicated
    # The final non-delta update doesn't re-add the text because dedupe bookkeeping
    # was updated during delta processing
    assert output.text == "Hello world!"
    assert output.error is None


def test_parse_message_response() -> None:
    payload = {
        "info": {"id": "turn-1", "error": "bad auth"},
        "parts": [{"type": "text", "text": "Hello"}],
    }
    result = parse_message_response(payload)
    assert result.text == "Hello"
    assert result.error == "bad auth"
