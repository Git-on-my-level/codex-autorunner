from __future__ import annotations

from codex_autorunner.agents.acp import (
    ACPOutputDeltaEvent,
    ACPPermissionRequestEvent,
    ACPTurnTerminalEvent,
    normalize_notification,
)


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
