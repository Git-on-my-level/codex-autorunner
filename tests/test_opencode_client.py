import json

from codex_autorunner.agents.opencode.client import (
    _normalize_sse_event,
    _normalize_template_path,
)
from codex_autorunner.agents.opencode.events import SSEEvent


def test_normalize_sse_event_unwraps_payload() -> None:
    event = SSEEvent(
        event="message",
        data=(
            '{"directory":"/repo","payload":{"type":"message.part.updated","properties":'
            '{"sessionID":"s1"}}}'
        ),
    )
    normalized = _normalize_sse_event(event)
    assert normalized.event == "message.part.updated"
    assert json.loads(normalized.data) == {
        "type": "message.part.updated",
        "properties": {"sessionID": "s1"},
    }


def test_normalize_sse_event_uses_payload_type() -> None:
    event = SSEEvent(
        event="message",
        data='{"type":"session.idle","sessionID":"s1"}',
    )
    normalized = _normalize_sse_event(event)
    assert normalized.event == "session.idle"
    assert json.loads(normalized.data) == {"type": "session.idle", "sessionID": "s1"}


def test_normalize_sse_event_keeps_non_json() -> None:
    event = SSEEvent(event="message", data="ping")
    normalized = _normalize_sse_event(event)
    assert normalized.event == "message"
    assert normalized.data == "ping"


def test_normalize_sse_event_preserves_wrapper_metadata() -> None:
    event = SSEEvent(
        event="message",
        data='{"type":"session.status","sessionID":"s42","payload":{"state":"running"}}',
    )
    normalized = _normalize_sse_event(event)
    payload = json.loads(normalized.data)
    assert payload["sessionID"] == "s42"
    assert payload.get("state") == "running"
    assert normalized.event == "session.status"


def test_normalize_template_path_matches_placeholder_names() -> None:
    normalized = _normalize_template_path("/session/{sessionID}/prompt_async")
    assert normalized == "/session/{}/prompt_async"
    # Different placeholder names should normalize identically
    assert normalized == _normalize_template_path("/session/{session_id}/prompt_async")
