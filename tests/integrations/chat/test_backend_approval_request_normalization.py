from __future__ import annotations

from codex_autorunner.integrations.chat.handlers.approvals import (
    NormalizedApprovalRequest,
    normalize_backend_approval_request,
)


def test_normalize_backend_approval_request_accepts_zero_id() -> None:
    assert normalize_backend_approval_request(
        {
            "id": 0,
            "params": {
                "turnId": "turn-1",
                "threadId": "thread-1",
            },
        }
    ) == NormalizedApprovalRequest(
        request_id="0",
        turn_id="turn-1",
        backend_thread_id="thread-1",
    )


def test_normalize_backend_approval_request_rejects_blank_id() -> None:
    assert (
        normalize_backend_approval_request(
            {
                "id": "",
                "params": {
                    "turnId": "turn-1",
                },
            }
        )
        is None
    )


def test_normalize_backend_approval_request_supports_snake_case_keys() -> None:
    assert normalize_backend_approval_request(
        {
            "id": "approval-1",
            "params": {
                "turn_id": "turn-2",
                "thread_id": "thread-2",
            },
        }
    ) == NormalizedApprovalRequest(
        request_id="approval-1",
        turn_id="turn-2",
        backend_thread_id="thread-2",
    )
