from __future__ import annotations

import pytest
from tests.acp_lifecycle_corpus import load_acp_lifecycle_corpus

from codex_autorunner.agents.acp import (
    ACPOutputDeltaEvent,
    ACPPermissionRequestEvent,
    ACPProgressEvent,
    ACPTurnTerminalEvent,
    normalize_notification,
)
from codex_autorunner.agents.acp.output_normalizer import normalize_acp_ingress_output


@pytest.mark.parametrize(
    ("case"),
    load_acp_lifecycle_corpus(),
    ids=[case["name"] for case in load_acp_lifecycle_corpus()],
)
def test_normalize_notification_shared_lifecycle_corpus(
    case: dict[str, object],
) -> None:
    raw = dict(case["raw"])  # type: ignore[index]
    expected = dict(case["expected"])  # type: ignore[index]

    event = normalize_notification(raw)

    assert event.kind == expected["normalized_kind"]
    assert event.method == raw["method"]
    if expected["normalized_kind"] == "turn_terminal":
        assert isinstance(event, ACPTurnTerminalEvent)
        assert event.status == expected["terminal_status"]
        assert event.final_output == expected["assistant_text"]
        assert event.error_message == expected["error_message"]
    elif expected["normalized_kind"] == "output_delta":
        assert isinstance(event, ACPOutputDeltaEvent)
        assert event.delta == expected["output_delta"]
    elif expected["normalized_kind"] == "progress":
        assert isinstance(event, ACPProgressEvent)
        assert event.message == expected["progress_message"]
    elif expected["normalized_kind"] == "permission_requested":
        assert isinstance(event, ACPPermissionRequestEvent)
        assert event.request_id == expected["permission_request_id"]
        assert event.description == expected["permission_description"]


def test_normalize_notification_maps_output_delta() -> None:
    event = normalize_notification(
        {
            "method": "prompt/progress",
            "params": {
                "sessionId": "session-1",
                "turnId": "turn-1",
                "delta": "hello",
            },
        }
    )

    assert isinstance(event, ACPOutputDeltaEvent)
    assert event.session_id == "session-1"
    assert event.turn_id == "turn-1"
    assert event.delta == "hello"


def test_normalize_acp_ingress_output_trims_transcript_projection() -> None:
    normalized = normalize_acp_ingress_output(
        "User:\nq1\n\nAssistant:\nA\n\nUser:\nq2\n\nAssistant:\nB",
        input_kind="current_turn_snapshot",
        prior_assistant_texts=("A",),
    )

    assert normalized.text == "B"
    assert normalized.classification == "transcript_projection"


def test_normalize_acp_ingress_output_keeps_within_turn_snapshot() -> None:
    normalized = normalize_acp_ingress_output(
        "Hello",
        input_kind="current_turn_snapshot",
        prior_assistant_texts=(),
    )

    assert normalized.text == "Hello"
    assert normalized.classification == "current_turn_snapshot"


def test_normalize_acp_ingress_output_trims_three_turn_cumulative_snapshot() -> None:
    normalized = normalize_acp_ingress_output(
        "first answer\n\nsecond answer\n\nthird answer",
        input_kind="current_turn_snapshot",
        prior_assistant_texts=("first answer", "second answer"),
    )

    assert normalized.text == "third answer"
    assert normalized.classification == "transcript_projection"
    assert normalized.matched_prior_chars == len("first answersecond answer")


def test_normalize_acp_ingress_output_rejects_prior_transcript_projection() -> None:
    normalized = normalize_acp_ingress_output(
        "User:\nq1\n\nAssistant:\nA",
        input_kind="current_turn_snapshot",
        prior_assistant_texts=("A",),
    )

    assert normalized.text == ""
    assert normalized.classification == "invalid_stale_output"


def test_normalize_acp_ingress_output_keeps_exact_prior_current_turn_snapshot() -> None:
    normalized = normalize_acp_ingress_output(
        "A",
        input_kind="current_turn_snapshot",
        prior_assistant_texts=("A",),
    )

    assert normalized.text == "A"
    assert normalized.classification == "current_turn_snapshot"


def test_normalize_notification_maps_permission_request_and_preserves_raw() -> None:
    raw = {
        "method": "permission/requested",
        "params": {
            "sessionId": "session-1",
            "turnId": "turn-1",
            "requestId": "perm-1",
            "description": "Need approval",
            "context": {"tool": "shell"},
        },
    }

    event = normalize_notification(raw)

    assert isinstance(event, ACPPermissionRequestEvent)
    assert event.request_id == "perm-1"
    assert event.description == "Need approval"
    assert event.context == {"tool": "shell"}
    assert event.raw_notification == raw


def test_normalize_notification_maps_request_style_permission_event() -> None:
    raw = {
        "id": "perm-9",
        "method": "session/request_permission",
        "params": {
            "sessionId": "session-1",
            "turnId": "turn-1",
            "description": "Need approval",
            "options": [
                {"optionId": "allow", "label": "Allow once"},
                {"optionId": "deny", "label": "Deny"},
            ],
            "context": {"tool": "shell", "command": ["ls"]},
        },
    }

    event = normalize_notification(raw)

    assert isinstance(event, ACPPermissionRequestEvent)
    assert event.request_id == "perm-9"
    assert event.description == "Need approval"
    assert event.context == {"tool": "shell", "command": ["ls"]}


def test_normalize_notification_maps_terminal_event() -> None:
    event = normalize_notification(
        {
            "method": "prompt/completed",
            "params": {
                "sessionId": "session-1",
                "turnId": "turn-1",
                "status": "completed",
                "finalOutput": "done",
            },
        }
    )

    assert isinstance(event, ACPTurnTerminalEvent)
    assert event.status == "completed"
    assert event.final_output == "done"


def test_normalize_notification_maps_terminal_event_with_agent_message_item() -> None:
    event = normalize_notification(
        {
            "method": "prompt/completed",
            "params": {
                "sessionId": "session-1",
                "turnId": "turn-1",
                "status": "completed",
                "item": {
                    "type": "agentMessage",
                    "text": "done from item",
                },
            },
        }
    )

    assert isinstance(event, ACPTurnTerminalEvent)
    assert event.status == "completed"
    assert event.final_output == "done from item"


def test_normalize_notification_maps_session_update_message_chunk() -> None:
    event = normalize_notification(
        {
            "method": "session/update",
            "params": {
                "sessionId": "session-1",
                "turnId": "turn-1",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "hello"},
                },
            },
        }
    )

    assert isinstance(event, ACPOutputDeltaEvent)
    assert event.session_id == "session-1"
    assert event.turn_id == "turn-1"
    assert event.delta == "hello"


def test_normalize_notification_preserves_hermes_cumulative_message_chunk() -> None:
    raw = {
        "method": "session/update",
        "params": {
            "sessionId": "session-1",
            "update": {
                "sessionUpdate": "agent_message_chunk",
                "content": {
                    "type": "text",
                    "text": "first answer\n\nsecond answer",
                },
            },
        },
    }

    event = normalize_notification(raw)

    # Root-cause conclusion: this shape is not CAR reading a progress field as
    # output; Hermes-style ACP sends cumulative text in the official assistant
    # message chunk payload.
    assert isinstance(event, ACPOutputDeltaEvent)
    assert event.delta == "first answer\n\nsecond answer"
    assert event.payload["update"]["sessionUpdate"] == "agent_message_chunk"


def test_normalize_notification_maps_session_update_message_chunk_with_content_parts() -> (
    None
):
    event = normalize_notification(
        {
            "method": "session/update",
            "params": {
                "sessionId": "session-1",
                "turnId": "turn-1",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": [
                        {"type": "text", "text": "hello"},
                        {"type": "output_text", "text": " world"},
                    ],
                },
            },
        }
    )

    assert isinstance(event, ACPOutputDeltaEvent)
    assert event.delta == "hello world"


def test_normalize_notification_maps_session_update_message_chunk_with_message_parts() -> (
    None
):
    event = normalize_notification(
        {
            "method": "session/update",
            "params": {
                "sessionId": "session-1",
                "turnId": "turn-1",
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": [
                        {"type": "message", "message": "hello"},
                        {"type": "message", "text": " world"},
                    ],
                },
            },
        }
    )

    assert isinstance(event, ACPOutputDeltaEvent)
    assert event.delta == "hello world"


def test_normalize_notification_maps_session_update_thought_chunk_with_string_content() -> (
    None
):
    event = normalize_notification(
        {
            "method": "session/update",
            "params": {
                "sessionId": "session-1",
                "turnId": "turn-1",
                "update": {
                    "sessionUpdate": "agent_thought_chunk",
                    "content": "thinking hard",
                },
            },
        }
    )

    assert isinstance(event, ACPProgressEvent)
    assert event.message == "thinking hard"


def test_normalize_notification_maps_session_idle_to_terminal_event() -> None:
    event = normalize_notification(
        {
            "method": "session.idle",
            "params": {
                "sessionId": "session-1",
                "turnId": "turn-1",
            },
        }
    )

    assert isinstance(event, ACPTurnTerminalEvent)
    assert event.status == "completed"
    assert event.turn_id == "turn-1"


def test_normalize_notification_maps_idle_session_status_to_terminal_event() -> None:
    event = normalize_notification(
        {
            "method": "session.status",
            "params": {
                "sessionId": "session-1",
                "turnId": "turn-1",
                "status": {"type": "idle"},
            },
        }
    )

    assert isinstance(event, ACPTurnTerminalEvent)
    assert event.status == "completed"
    assert event.turn_id == "turn-1"


def test_normalize_notification_maps_busy_session_status_to_progress_event() -> None:
    event = normalize_notification(
        {
            "method": "session.status",
            "params": {
                "sessionId": "session-1",
                "turnId": "turn-1",
                "status": {"type": "running"},
            },
        }
    )

    assert isinstance(event, ACPProgressEvent)
    assert event.message == "running"
    assert event.turn_id == "turn-1"
