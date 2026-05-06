from __future__ import annotations

import pytest

from codex_autorunner.surfaces.web.routes.pma_routes.managed_thread_tail_serializers import (
    build_managed_thread_stream_lifecycle,
)


@pytest.mark.parametrize(
    ("name", "inputs", "expected"),
    [
        (
            "running",
            {
                "managed_turn_id": "turn-1",
                "turn_status": "running",
                "thread_status": "running",
                "lifecycle_status": "active",
                "stream_available": True,
            },
            {
                "work_status": "running",
                "operator_status": "running",
                "terminal": False,
                "stream_should_close": False,
                "stream_close_reason": None,
            },
        ),
        (
            "queued",
            {
                "managed_turn_id": "turn-1",
                "turn_status": "queued",
                "thread_status": "running",
                "lifecycle_status": "active",
                "stream_available": False,
            },
            {
                "work_status": "queued",
                "terminal": False,
                "stream_should_close": True,
                "stream_close_reason": "queued",
            },
        ),
        (
            "ok",
            {
                "managed_turn_id": "turn-1",
                "turn_status": "ok",
                "thread_status": "completed",
                "lifecycle_status": "active",
                "stream_available": False,
            },
            {
                "work_status": "ok",
                "operator_status": "reusable",
                "terminal": True,
                "stream_should_close": True,
                "stream_close_reason": "terminal:ok",
            },
        ),
        (
            "error",
            {
                "managed_turn_id": "turn-1",
                "turn_status": "error",
                "thread_status": "failed",
                "lifecycle_status": "active",
                "stream_available": False,
            },
            {
                "work_status": "error",
                "operator_status": "attention_required",
                "terminal": True,
                "stream_should_close": True,
                "stream_close_reason": "terminal:error",
            },
        ),
        (
            "failed",
            {
                "managed_turn_id": "turn-1",
                "turn_status": "failed",
                "thread_status": "failed",
                "lifecycle_status": "active",
                "stream_available": False,
            },
            {
                "work_status": "failed",
                "operator_status": "attention_required",
                "terminal": True,
                "stream_should_close": True,
                "stream_close_reason": "terminal:failed",
            },
        ),
        (
            "interrupted",
            {
                "managed_turn_id": "turn-1",
                "turn_status": "interrupted",
                "thread_status": "interrupted",
                "lifecycle_status": "active",
                "stream_available": False,
            },
            {
                "work_status": "interrupted",
                "operator_status": "reusable",
                "terminal": True,
                "stream_should_close": True,
                "stream_close_reason": "terminal:interrupted",
            },
        ),
        (
            "no-running-turn",
            {
                "managed_turn_id": None,
                "turn_status": None,
                "thread_status": "idle",
                "lifecycle_status": "active",
                "stream_available": False,
            },
            {
                "work_status": "idle",
                "operator_status": "idle",
                "terminal": False,
                "stream_should_close": True,
                "stream_close_reason": "no_running_turn",
            },
        ),
        (
            "non-streamable-running",
            {
                "managed_turn_id": "turn-1",
                "turn_status": "running",
                "thread_status": "running",
                "lifecycle_status": "active",
                "stream_available": False,
            },
            {
                "work_status": "running",
                "operator_status": "running",
                "terminal": False,
                "stream_should_close": False,
                "stream_close_reason": None,
            },
        ),
    ],
)
def test_managed_thread_stream_lifecycle_contract(
    name: str, inputs: dict[str, object], expected: dict[str, object]
) -> None:
    _ = name
    payload = build_managed_thread_stream_lifecycle(**inputs)

    for key, value in expected.items():
        assert payload[key] == value
