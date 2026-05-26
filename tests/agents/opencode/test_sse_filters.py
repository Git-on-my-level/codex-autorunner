from __future__ import annotations

import pytest

from codex_autorunner.agents.opencode.sse_filters import opencode_sse_event_is_noise
from codex_autorunner.core.orchestration.runtime_thread_decoders import (
    OpenCodeMessageDecoder,
)
from codex_autorunner.core.orchestration.runtime_thread_events import (
    RuntimeThreadRunEventState,
    normalize_runtime_thread_message,
)
from codex_autorunner.core.ports.run_event import RunNotice


@pytest.mark.parametrize(
    ("event_type", "expected"),
    [
        ("server.connected", True),
        ("server.heartbeat", True),
        ("sync", True),
        ("session.next.agent.switched", True),
        ("session.next.model.switched", True),
        ("message.part.updated", False),
        ("message.part.delta", False),
        ("session.status", False),
        ("tool_call", False),
    ],
)
def test_opencode_sse_event_is_noise(event_type: str, expected: bool) -> None:
    assert opencode_sse_event_is_noise(event_type) is expected


def test_reasoning_snapshot_suppresses_prefix_growth() -> None:
    state = RuntimeThreadRunEventState()
    base_params = {
        "messageID": "msg-1",
        "properties": {
            "part": {
                "id": "part-1",
                "type": "reasoning",
                "messageID": "msg-1",
            }
        },
    }
    first = normalize_runtime_thread_message(
        "message.part.updated",
        {
            **base_params,
            "properties": {
                "part": {**base_params["properties"]["part"], "text": "Hello"}
            },
        },
        state,
    )
    grown = normalize_runtime_thread_message(
        "message.part.updated",
        {
            **base_params,
            "properties": {
                "part": {**base_params["properties"]["part"], "text": "Hello world"}
            },
        },
        state,
    )
    assert len(first) == 1
    assert grown == []


def test_reasoning_snapshot_emits_once_until_content_changes() -> None:
    state = RuntimeThreadRunEventState()
    params = {
        "messageID": "msg-1",
        "properties": {
            "part": {
                "id": "part-1",
                "type": "reasoning",
                "text": "Hello",
                "messageID": "msg-1",
            }
        },
    }
    first = normalize_runtime_thread_message("message.part.updated", params, state)
    second = normalize_runtime_thread_message("message.part.updated", params, state)
    assert len(first) == 1
    assert isinstance(first[0], RunNotice)
    assert first[0].kind == "thinking"
    assert first[0].message == "Hello"
    assert second == []


def test_message_part_delta_is_ignored_for_opencode() -> None:
    decoder = OpenCodeMessageDecoder()
    state = RuntimeThreadRunEventState()
    from tests.core.orchestration.test_runtime_thread_decoders import _ctx

    ctx_state, ctx = _ctx(
        "message.part.delta",
        {
            "properties": {
                "part": {
                    "id": "part-1",
                    "type": "reasoning",
                    "delta": {"text": "x"},
                    "messageID": "msg-1",
                }
            }
        },
        state=state,
    )
    events = decoder.decode(
        "message.part.delta",
        {
            "properties": {
                "part": {
                    "id": "part-1",
                    "type": "reasoning",
                    "delta": {"text": "x"},
                    "messageID": "msg-1",
                }
            }
        },
        ctx_state,
        ctx,
    )
    assert events == []
