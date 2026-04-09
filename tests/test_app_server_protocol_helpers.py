from codex_autorunner.integrations.app_server.protocol_helpers import (
    normalize_approval_request,
    normalize_notification_envelope,
    normalize_response,
)
from codex_autorunner.integrations.app_server.protocol_types import (
    ApprovalRequest,
    TurnCompletedNotification,
)


def test_normalize_response_preserves_contract_shape() -> None:
    response = normalize_response(
        {
            "id": 7,
            "result": {"ok": True},
            "error": {"code": -32000, "message": "ignored when present"},
        }
    )

    assert response is not None
    assert response.request_id == "7"
    assert response.result == {"ok": True}
    assert response.error == {"code": -32000, "message": "ignored when present"}


def test_normalize_approval_request_builds_typed_envelope() -> None:
    raw = {
        "id": "req-1",
        "method": "item/commandExecution/requestApproval",
        "params": {
            "turnId": "turn-1",
            "threadId": "thread-1",
            "itemId": "item-1",
            "message": "Need approval",
        },
    }

    envelope = normalize_approval_request(raw)

    assert envelope is not None
    assert envelope.request_id == "req-1"
    assert envelope.method == "item/commandExecution/requestApproval"
    assert envelope.params["threadId"] == "thread-1"
    assert envelope.raw_message == raw
    assert isinstance(envelope.request, ApprovalRequest)
    assert envelope.request.turn_id == "turn-1"
    assert envelope.request.thread_id == "thread-1"
    assert envelope.request.item_id == "item-1"
    assert envelope.request.context == raw["params"]


def test_normalize_notification_envelope_builds_typed_turn_completion() -> None:
    raw = {
        "method": "turn/completed",
        "params": {
            "turnId": "turn-1",
            "threadId": "thread-1",
            "status": "completed",
        },
    }

    envelope = normalize_notification_envelope(raw)

    assert envelope is not None
    assert envelope.method == "turn/completed"
    assert envelope.params == raw["params"]
    assert envelope.raw_message == raw
    assert isinstance(envelope.notification, TurnCompletedNotification)
    assert envelope.notification.turn_id == "turn-1"
    assert envelope.notification.thread_id == "thread-1"
    assert envelope.notification.status == "completed"
