import asyncio
import json
import time

import pytest

from codex_autorunner.agents.opencode import runtime as opencode_runtime
from codex_autorunner.agents.opencode.runtime import (
    collect_opencode_output,
    collect_opencode_output_from_events,
    extract_session_id,
    opencode_stream_timeouts,
    parse_message_response,
    recover_last_assistant_message,
)
from codex_autorunner.core.sse import SSEEvent


async def _iter_events(events):
    for event in events:
        yield event


def test_extract_session_id_prefers_nested_session_id() -> None:
    payload = {"session": {"id": "session-xyz"}}
    assert extract_session_id(payload) == "session-xyz"


def test_extract_session_id_reads_nested_item_session_id() -> None:
    payload = {"item": {"sessionID": "session-xyz"}}
    assert extract_session_id(payload) == "session-xyz"


def test_extract_session_id_reads_properties_item_nested_session_object() -> None:
    payload = {"properties": {"item": {"session": {"id": "session-xyz"}}}}
    assert extract_session_id(payload) == "session-xyz"


def test_extract_session_id_preserves_existing_precedence_over_nested_item() -> None:
    payload = {
        "sessionID": "session-top",
        "properties": {"item": {"sessionID": "session-item"}},
    }
    assert extract_session_id(payload) == "session-top"


def test_opencode_stream_timeouts_caps_first_event_to_stall_when_lower() -> None:
    stall, first = opencode_stream_timeouts(30.0)
    assert stall == 30.0
    assert first == 30.0


def test_opencode_stream_timeouts_default_stall_when_none() -> None:
    stall, first = opencode_stream_timeouts(None)
    assert stall == opencode_runtime._OPENCODE_STREAM_STALL_TIMEOUT_SECONDS
    assert first == opencode_runtime._OPENCODE_FIRST_EVENT_TIMEOUT_SECONDS


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
async def test_collect_output_fallback_message_excludes_reasoning_content() -> None:
    """When streaming yields no text and message.completed carries reasoning+text in content, only text should appear."""
    events = [
        SSEEvent(
            event="message.part.delta",
            data='{"sessionID":"s1","properties":{"delta":{"text":"thinking hard"},"part":{"id":"r1","type":"reasoning","text":"thinking hard"}}}',
        ),
        SSEEvent(
            event="message.completed",
            data=json.dumps(
                {
                    "sessionID": "s1",
                    "properties": {
                        "info": {"id": "m1", "role": "assistant"},
                    },
                    "content": [
                        {"type": "reasoning", "text": "thinking hard"},
                        {"type": "text", "text": "the answer"},
                    ],
                }
            ),
        ),
        SSEEvent(event="session.idle", data='{"sessionID":"s1"}'),
    ]
    output = await collect_opencode_output_from_events(
        _iter_events(events),
        session_id="s1",
    )
    assert output.text == "the answer"
    assert "thinking" not in output.text.lower()


@pytest.mark.anyio
async def test_collect_output_supports_message_part_delta_with_direct_ids() -> None:
    events = [
        SSEEvent(
            event="message.updated",
            data='{"sessionID":"s1","properties":{"info":{"id":"assistant-1","role":"assistant"}}}',
        ),
        SSEEvent(
            event="message.part.updated",
            data='{"sessionID":"s1","properties":{"delta":{"text":"Hello "},'
            '"part":{"id":"p1","messageID":"assistant-1","type":"text","text":"Hello "}}}',
        ),
        SSEEvent(
            event="message.part.delta",
            data='{"sessionID":"s1","properties":{"partID":"p1","messageID":"assistant-1",'
            '"delta":{"text":"world"}}}',
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
async def test_collect_output_uses_part_type_memory_for_reasoning_delta_events() -> (
    None
):
    seen_reasoning: list[str] = []

    async def _part_handler(part_type: str, part: dict[str, str], delta_text):
        if part_type == "reasoning" and isinstance(delta_text, str):
            seen_reasoning.append(delta_text)

    events = [
        SSEEvent(
            event="message.part.updated",
            data='{"sessionID":"s1","properties":{"delta":{"text":"thinking"},"part":{"id":"r1","type":"reasoning"}}}',
        ),
        SSEEvent(
            event="message.part.delta",
            data='{"sessionID":"s1","properties":{"partID":"r1","delta":{"text":" more"}}}',
        ),
        SSEEvent(event="session.idle", data='{"sessionID":"s1"}'),
    ]
    output = await collect_opencode_output_from_events(
        _iter_events(events),
        session_id="s1",
        part_handler=_part_handler,
    )
    assert output.text == ""
    assert output.error is None
    assert seen_reasoning == ["thinking", " more"]


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
async def test_collect_output_auto_responds_permission_request() -> None:
    replies = []

    async def _respond(request_id: str, reply: str) -> None:
        replies.append((request_id, reply))

    events = [
        SSEEvent(
            event="permission.asked",
            data='{"sessionID":"s1","properties":{"id":"perm-1","tool":"shell","command":"rm -rf tmp"}}',
        ),
        SSEEvent(event="session.idle", data='{"sessionID":"s1"}'),
    ]
    output = await collect_opencode_output_from_events(
        _iter_events(events),
        session_id="s1",
        respond_permission=_respond,
    )

    assert output.text == ""
    assert output.error is None
    assert replies == [("perm-1", "once")]


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
async def test_collect_output_emits_usage_from_message_part_tokens() -> None:
    seen: list[dict[str, int]] = []

    async def _part_handler(part_type: str, part: dict[str, int], delta_text):
        if part_type == "usage":
            seen.append(part)

    events = [
        SSEEvent(
            event="message.part.updated",
            data=(
                '{"sessionID":"s1","properties":{"part":{"type":"step-finish",'
                '"tokens":{"input":11,"output":4,"reasoning":1,'
                '"cache":{"read":2}},"modelContextWindow":4096}}}'
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
    assert usage["inputTokens"] == 11
    assert usage["cachedInputTokens"] == 2
    assert usage["outputTokens"] == 4
    assert usage["reasoningTokens"] == 1
    assert usage["modelContextWindow"] == 4096


@pytest.mark.anyio
async def test_collect_output_dedupes_usage_between_part_and_message_events() -> None:
    seen: list[dict[str, int]] = []

    async def _part_handler(part_type: str, part: dict[str, int], delta_text):
        if part_type == "usage":
            seen.append(part)

    events = [
        SSEEvent(
            event="message.part.updated",
            data=(
                '{"sessionID":"s1","properties":{"part":{"type":"step-finish",'
                '"tokens":{"input":10,"output":5},"modelContextWindow":2000}}}'
            ),
        ),
        SSEEvent(
            event="message.updated",
            data=(
                '{"sessionID":"s1","properties":{"info":{"tokens":'
                '{"input":10,"output":5},"modelContextWindow":2000}}}'
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


@pytest.mark.anyio
async def test_collect_output_emits_usage_when_detail_breakdown_changes() -> None:
    seen: list[dict[str, int]] = []

    async def _part_handler(part_type: str, part: dict[str, int], delta_text):
        if part_type == "usage":
            seen.append(part)

    events = [
        SSEEvent(
            event="message.part.updated",
            data=(
                '{"sessionID":"s1","properties":{"part":{"type":"step-finish",'
                '"tokens":{"input":12,"output":3},"modelContextWindow":2000}}}'
            ),
        ),
        SSEEvent(
            event="message.updated",
            data=(
                '{"sessionID":"s1","properties":{"info":{"tokens":'
                '{"input":10,"output":3,"cache":{"read":2}},'
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
    assert len(seen) == 2
    assert seen[0]["totalTokens"] == 15
    assert "cachedInputTokens" not in seen[0]
    assert seen[1]["totalTokens"] == 15
    assert seen[1]["cachedInputTokens"] == 2


@pytest.mark.anyio
async def test_collect_output_returns_latest_usage_snapshot_without_handler() -> None:
    events = [
        SSEEvent(
            event="message.part.updated",
            data=(
                '{"sessionID":"s1","properties":{"part":{"type":"step-finish",'
                '"tokens":{"input":10,"output":5}}}}'
            ),
        ),
        SSEEvent(
            event="message.updated",
            data=(
                '{"sessionID":"s1","properties":{"info":{"tokens":'
                '{"input":10,"output":5,"cache":{"read":2}}}}}'
            ),
        ),
        SSEEvent(event="session.idle", data='{"sessionID":"s1"}'),
    ]
    output = await collect_opencode_output_from_events(
        _iter_events(events),
        session_id="s1",
    )
    assert output.text == ""
    assert output.error is None
    assert output.usage is not None
    assert output.usage["totalTokens"] == 17
    assert output.usage["cachedInputTokens"] == 2


@pytest.mark.anyio
async def test_collect_output_prefers_aggregate_usage_over_part_tokens() -> None:
    seen: list[dict[str, int]] = []

    async def _part_handler(part_type: str, part: dict[str, int], delta_text):
        if part_type == "usage":
            seen.append(part)

    events = [
        SSEEvent(
            event="message.part.updated",
            data=(
                '{"sessionID":"s1","properties":{"info":{"tokens":'
                '{"input":100,"output":50}},"part":{"type":"step-finish",'
                '"tokens":{"input":1,"output":1}}}}'
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
    assert seen[0]["totalTokens"] == 150
    assert seen[0]["inputTokens"] == 100
    assert seen[0]["outputTokens"] == 50


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
async def test_collect_output_exits_after_completion_without_idle_when_stream_stays_busy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _busy_after_completion():
        yield SSEEvent(
            event="message.completed",
            data='{"sessionID":"s1","info":{"id":"m1","role":"assistant"},'
            '"parts":[{"type":"text","text":"Hello"}]}',
        )
        while True:
            await asyncio.sleep(0.002)
            yield SSEEvent(
                event="session.status",
                data='{"sessionID":"s1","status":{"type":"busy"}}',
            )

    monkeypatch.setattr(
        opencode_runtime,
        "_OPENCODE_POST_COMPLETION_GRACE_SECONDS",
        0.01,
    )

    start = time.monotonic()
    output = await collect_opencode_output_from_events(
        None,
        session_id="s1",
        event_stream_factory=lambda: _busy_after_completion(),
        stall_timeout_seconds=30.0,
    )
    elapsed = time.monotonic() - start

    assert elapsed < 0.5
    assert output.text == "Hello"
    assert output.error is None


@pytest.mark.anyio
async def test_collect_output_does_not_start_grace_on_user_completion(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _user_then_assistant_completion():
        yield SSEEvent(
            event="message.completed",
            data='{"sessionID":"s1","info":{"id":"u1","role":"user"}}',
        )
        for _ in range(10):
            await asyncio.sleep(0.002)
            yield SSEEvent(
                event="session.status",
                data='{"sessionID":"s1","status":{"type":"busy"}}',
            )
        yield SSEEvent(
            event="message.completed",
            data='{"sessionID":"s1","info":{"id":"a1","role":"assistant"},'
            '"parts":[{"type":"text","text":"Hello"}]}',
        )
        yield SSEEvent(event="session.idle", data='{"sessionID":"s1"}')

    monkeypatch.setattr(
        opencode_runtime,
        "_OPENCODE_POST_COMPLETION_GRACE_SECONDS",
        0.01,
    )

    output = await collect_opencode_output_from_events(
        None,
        session_id="s1",
        event_stream_factory=lambda: _user_then_assistant_completion(),
        stall_timeout_seconds=30.0,
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
        prompt="User prompt",
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
async def test_collect_output_drops_user_prompt_without_message_ids() -> None:
    events = [
        # User prompt arrives without a message id; should not be echoed.
        SSEEvent(
            event="message.part.updated",
            data='{"sessionID":"s1","properties":{"delta":{"text":"User prompt"},'
            '"part":{"type":"text","text":"User prompt"}}}',
        ),
        SSEEvent(
            event="message.completed",
            data='{"sessionID":"s1","info":{"id":"u1","role":"user"}}',
        ),
        # Assistant reply also lacks a message id; should be preserved.
        SSEEvent(
            event="message.part.updated",
            data='{"sessionID":"s1","properties":{"delta":{"text":"Assistant reply"},'
            '"part":{"type":"text","text":"Assistant reply"}}}',
        ),
        SSEEvent(
            event="message.completed",
            data='{"sessionID":"s1","info":{"id":"a1","role":"assistant"}}',
        ),
        SSEEvent(event="session.idle", data='{"sessionID":"s1"}'),
    ]
    output = await collect_opencode_output_from_events(
        _iter_events(events),
        session_id="s1",
    )
    assert output.text == "Assistant reply"
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


def test_parse_message_response_skips_reasoning_in_content_list() -> None:
    payload = {
        "content": [
            {"type": "reasoning", "text": "chain of thought"},
            {"type": "text", "text": "User-facing"},
        ],
    }
    result = parse_message_response(payload)
    assert result.text == "User-facing"
    assert result.error is None


@pytest.mark.anyio
async def test_collect_output_poll_treats_missing_status_as_idle() -> None:
    """Stall recovery should treat a missing session status entry as idle and finish."""

    class _FakeClient:
        def __init__(self):
            self.session_status_calls = 0

        def stream_events(
            self, *, directory=None, ready_event=None, paths=None, session_id=None
        ):
            async def _gen():
                while True:
                    await asyncio.sleep(3600)
                    yield SSEEvent(event="keepalive", data="{}")

            return _gen()

        async def session_status(self, *, directory):
            self.session_status_calls += 1
            # Simulate OpenCode's sparse status map: session missing => idle
            return {}

        async def respond_permission(self, **kwargs):
            return None

        async def reply_question(self, *args, **kwargs):
            return None

        async def reject_question(self, *args, **kwargs):
            return None

        async def providers(self, **kwargs):
            return {}

    client = _FakeClient()
    start = time.monotonic()
    output = await collect_opencode_output(
        client,
        session_id="s1",
        workspace_path=".",
        stall_timeout_seconds=0.01,
        first_event_timeout_seconds=None,
    )
    elapsed = time.monotonic() - start

    # Should exit quickly via polling path and not hang indefinitely
    assert elapsed < 0.5
    assert output.text == ""
    assert output.error is None
    assert client.session_status_calls >= 1


@pytest.mark.anyio
async def test_collect_output_uses_session_scoped_stream_endpoint() -> None:
    class _FakeClient:
        def __init__(self):
            self.stream_calls: list[dict[str, object]] = []

        def stream_events(
            self, *, directory=None, ready_event=None, paths=None, session_id=None
        ):
            self.stream_calls.append(
                {
                    "directory": directory,
                    "ready_event": ready_event,
                    "paths": paths,
                    "session_id": session_id,
                }
            )

            async def _gen():
                if ready_event is not None:
                    ready_event.set()
                yield SSEEvent(event="session.idle", data='{"sessionID":"s1"}')

            return _gen()

        async def session_status(self, *, directory):
            return {"s1": {"type": "idle"}}

        async def respond_permission(self, **kwargs):
            return None

        async def reply_question(self, *args, **kwargs):
            return None

        async def reject_question(self, *args, **kwargs):
            return None

        async def providers(self, **kwargs):
            return {}

    client = _FakeClient()
    output = await collect_opencode_output(
        client,
        session_id="s1",
        workspace_path=".",
    )

    assert output.text == ""
    assert output.error is None
    assert client.stream_calls == [
        {
            "directory": ".",
            "ready_event": None,
            "paths": None,
            "session_id": "s1",
        }
    ]


@pytest.mark.anyio
async def test_collect_output_times_out_waiting_for_first_relevant_event() -> None:
    statuses: list[dict[str, object]] = []
    progress_events: list[dict[str, object]] = []

    async def _status_fetcher():
        statuses.append({"status": None})
        return {"status": None}

    async def _part_handler(part_type: str, part: dict[str, object], delta_text):
        if part_type == "status":
            progress_events.append(part)
        return None

    async def _never_event_stream():
        while True:
            await asyncio.sleep(3600)
            yield SSEEvent(event="keepalive", data="{}")

    start = time.monotonic()
    output = await collect_opencode_output_from_events(
        None,
        session_id="s1",
        event_stream_factory=lambda: _never_event_stream(),
        session_fetcher=_status_fetcher,
        part_handler=_part_handler,
        stall_timeout_seconds=30.0,
        first_event_timeout_seconds=0.001,
    )
    elapsed = time.monotonic() - start

    assert elapsed < 0.5
    assert output.text == ""
    assert output.error is not None
    assert "opencode_first_event_timeout" in output.error
    assert len(statuses) >= 1
    assert any(
        event.get("reason") == "opencode_first_event_timeout"
        for event in progress_events
    )


@pytest.mark.anyio
async def test_collect_output_bounds_stall_reconnect_loop_if_session_missing(
    monkeypatch,
) -> None:
    statuses: list[dict[str, object]] = []
    progress_events: list[dict[str, object]] = []

    async def _status_fetcher():
        statuses.append({"status": None})
        return {"status": None}

    async def _part_handler(part_type: str, part: dict[str, object], delta_text):
        if part_type == "status":
            progress_events.append(part)
        return None

    async def _never_event_stream():
        while True:
            await asyncio.sleep(3600)
            yield SSEEvent(event="keepalive", data="{}")

    monkeypatch.setattr(
        opencode_runtime,
        "_OPENCODE_STREAM_RECONNECT_BACKOFF_SECONDS",
        (0.0, 0.0),
    )
    monkeypatch.setattr(
        opencode_runtime,
        "_OPENCODE_STREAM_MAX_STALL_RECONNECT_ATTEMPTS",
        2,
    )
    monkeypatch.setattr(
        opencode_runtime,
        "_OPENCODE_STREAM_MAX_STALL_RECONNECT_SECONDS",
        999.0,
    )

    output = await collect_opencode_output_from_events(
        None,
        session_id="s1",
        event_stream_factory=lambda: _never_event_stream(),
        session_fetcher=_status_fetcher,
        part_handler=_part_handler,
        stall_timeout_seconds=0.001,
        first_event_timeout_seconds=None,
    )

    assert output.text == ""
    assert output.error is not None
    assert "opencode_stream_stalled_timeout" in output.error
    assert len(statuses) >= 1
    assert any(event.get("type") == "reconnecting" for event in progress_events)
    assert any(event.get("type") == "stall_timeout" for event in progress_events)


@pytest.mark.anyio
async def test_collect_output_waits_if_session_busy(monkeypatch) -> None:
    statuses: list[dict[str, object]] = []
    progress_events: list[dict[str, object]] = []

    async def _status_fetcher():
        print(f"Fetching status, count is {len(statuses)}")
        # Return busy a few times, then idle
        if len(statuses) < 3:
            statuses.append({"status": {"type": "busy"}})
            return {"status": {"type": "busy"}}
        statuses.append({"status": {"type": "idle"}})
        return {"status": {"type": "idle"}}

    async def _part_handler(part_type: str, part: dict[str, object], delta_text):
        if part_type == "status":
            progress_events.append(part)
        return None

    async def _never_event_stream():
        while True:
            await asyncio.sleep(3600)
            yield SSEEvent(event="keepalive", data="{}")

    monkeypatch.setattr(
        opencode_runtime,
        "_OPENCODE_STREAM_RECONNECT_BACKOFF_SECONDS",
        (0.0, 0.0),
    )
    monkeypatch.setattr(
        opencode_runtime,
        "_OPENCODE_STREAM_MAX_STALL_RECONNECT_ATTEMPTS",
        2,
    )
    monkeypatch.setattr(
        opencode_runtime,
        "_OPENCODE_STREAM_MAX_STALL_RECONNECT_SECONDS",
        999.0,
    )

    # Patch the sleep inside opencode_runtime so the test runs fast
    original_sleep = asyncio.sleep
    monkeypatch.setattr(
        opencode_runtime.asyncio, "sleep", lambda x: original_sleep(0.001)
    )

    output = await collect_opencode_output_from_events(
        None,
        session_id="s1",
        event_stream_factory=lambda: _never_event_stream(),
        session_fetcher=_status_fetcher,
        part_handler=_part_handler,
        stall_timeout_seconds=0.001,
        first_event_timeout_seconds=None,
    )

    assert output.text == ""
    assert output.error is None
    assert len(statuses) == 4


@pytest.mark.anyio
async def test_collect_output_uses_stall_timeout_after_first_relevant_event(
    monkeypatch,
) -> None:
    statuses: list[dict[str, object]] = []
    progress_events: list[dict[str, object]] = []
    state = {"sent_first_event": False}

    async def _status_fetcher():
        statuses.append({"status": None})
        return {"status": None}

    async def _part_handler(part_type: str, part: dict[str, object], delta_text):
        if part_type == "status":
            progress_events.append(part)
        return None

    async def _event_then_hang():
        if not state["sent_first_event"]:
            state["sent_first_event"] = True
            # Busy session.status is not a progress signal (see
            # opencode_event_is_progress_signal); use a content event so stall
            # timeout applies after the stream goes quiet.
            yield SSEEvent(
                event="message.updated",
                data='{"sessionID":"s1","info":{"id":"m1","role":"assistant"}}',
            )
        while True:
            await asyncio.sleep(3600)
            yield SSEEvent(event="keepalive", data="{}")

    monkeypatch.setattr(
        opencode_runtime,
        "_OPENCODE_STREAM_RECONNECT_BACKOFF_SECONDS",
        (0.0, 0.0),
    )
    monkeypatch.setattr(
        opencode_runtime,
        "_OPENCODE_STREAM_MAX_STALL_RECONNECT_ATTEMPTS",
        2,
    )
    monkeypatch.setattr(
        opencode_runtime,
        "_OPENCODE_STREAM_MAX_STALL_RECONNECT_SECONDS",
        999.0,
    )

    output = await collect_opencode_output_from_events(
        None,
        session_id="s1",
        event_stream_factory=lambda: _event_then_hang(),
        session_fetcher=_status_fetcher,
        part_handler=_part_handler,
        stall_timeout_seconds=0.001,
        first_event_timeout_seconds=30.0,
    )

    assert output.text == ""
    assert output.error is not None
    assert "opencode_stream_stalled_timeout" in output.error
    assert len(statuses) >= 1
    assert any(event.get("type") == "reconnecting" for event in progress_events)
    assert not any(
        event.get("reason") == "opencode_first_event_timeout"
        for event in progress_events
    )


@pytest.mark.anyio
async def test_collect_output_sessionless_text_delta_is_relevant() -> None:
    """Sessionless text deltas should be treated as relevant and collected."""
    events = [
        SSEEvent(
            event="message.part.updated",
            data='{"properties":{"delta":{"text":"Hello"},'
            '"part":{"type":"text","text":"Hello"}}}',
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
async def test_collect_output_sessionless_idle_does_not_end_turn() -> None:
    """A sessionless idle must not complete the turn before session-scoped content."""
    events = [
        SSEEvent(event="session.idle", data="{}"),
        SSEEvent(
            event="message.part.updated",
            data='{"properties":{"delta":{"text":"Hello"},'
            '"part":{"type":"text","text":"Hello"}}}',
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
async def test_collect_output_sessionless_completed_recovers_roleless_text() -> None:
    """A sessionless message.completed with no role should still produce text
    when the text differs from the prompt."""
    events = [
        SSEEvent(
            event="message.completed",
            data='{"info":{"id":"m1"},'
            '"parts":[{"type":"text","text":"Final answer"}]}',
        ),
        SSEEvent(event="session.idle", data='{"sessionID":"s1"}'),
    ]
    output = await collect_opencode_output_from_events(
        _iter_events(events),
        session_id="s1",
        prompt="What is the answer?",
    )
    assert output.text == "Final answer"
    assert output.error is None


@pytest.mark.anyio
async def test_collect_output_sessionless_completed_suppresses_echoed_prompt() -> None:
    """A sessionless roleless message.completed whose text matches the prompt
    should NOT produce output (would be an echo)."""
    events = [
        SSEEvent(
            event="message.completed",
            data='{"info":{"id":"m1"},'
            '"parts":[{"type":"text","text":"What is the answer?"}]}',
        ),
        SSEEvent(event="session.idle", data='{"sessionID":"s1"}'),
    ]
    output = await collect_opencode_output_from_events(
        _iter_events(events),
        session_id="s1",
        prompt="What is the answer?",
    )
    assert output.text == ""
    assert output.error is None


@pytest.mark.anyio
async def test_collect_output_recovers_final_text_from_session_messages() -> None:
    events = [
        SSEEvent(event="session.idle", data='{"sessionID":"s1"}'),
    ]

    async def _messages_fetcher():
        return [
            {
                "info": {"id": "user-1", "role": "user"},
                "parts": [{"type": "text", "text": "Can you echo hello world?"}],
            },
            {
                "info": {"id": "assistant-1", "role": "assistant"},
                "parts": [{"type": "text", "text": "hello world"}],
            },
        ]

    output = await collect_opencode_output_from_events(
        _iter_events(events),
        session_id="s1",
        prompt="Can you echo hello world?",
        messages_fetcher=_messages_fetcher,
    )

    assert output.text == "hello world"
    assert output.error is None


def test_recover_last_assistant_message_ignores_ignored_text_parts() -> None:
    payload = [
        {
            "info": {"id": "assistant-1", "role": "assistant"},
            "parts": [
                {"type": "text", "text": "user echo", "ignored": True},
                {"type": "text", "text": "final answer"},
            ],
        }
    ]
    result = recover_last_assistant_message(payload, prompt="user echo")
    assert result.text == "final answer"
    assert result.error is None
