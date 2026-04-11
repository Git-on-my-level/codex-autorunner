from __future__ import annotations

from pathlib import Path

import pytest

from codex_autorunner.core.orchestration.cold_trace_store import ColdTraceStore
from codex_autorunner.core.orchestration.runtime_thread_events import (
    RuntimeThreadRunEventState,
    normalize_runtime_thread_raw_event,
)
from codex_autorunner.core.orchestration.turn_timeline import (
    list_turn_timeline,
    persist_turn_timeline,
)
from codex_autorunner.core.ports.run_event import (
    Completed,
    RunNotice,
    ToolCall,
    ToolResult,
)


def test_persist_turn_timeline_round_trips_events(tmp_path: Path) -> None:
    count = persist_turn_timeline(
        tmp_path,
        execution_id="turn-1",
        target_kind="thread_target",
        target_id="thread-1",
        repo_id="repo-1",
        metadata={"agent": "codex", "backend_turn_id": "backend-1"},
        events=[
            RunNotice(
                timestamp="2026-03-19T00:00:00Z",
                kind="thinking",
                message="planning",
            ),
            ToolCall(
                timestamp="2026-03-19T00:00:01Z",
                tool_name="shell",
                tool_input={"cmd": "pwd"},
            ),
            ToolResult(
                timestamp="2026-03-19T00:00:02Z",
                tool_name="shell",
                status="completed",
                result={"stdout": "/tmp"},
            ),
            Completed(
                timestamp="2026-03-19T00:00:03Z",
                final_message="done",
            ),
        ],
    )

    assert count == 4
    timeline = list_turn_timeline(tmp_path, execution_id="turn-1")
    assert [entry["event_type"] for entry in timeline] == [
        "run_notice",
        "tool_call",
        "tool_result",
        "turn_completed",
    ]


@pytest.mark.anyio
async def test_persist_turn_timeline_coalesces_cumulative_reasoning_updates_into_hot_deltas(
    tmp_path: Path,
) -> None:
    state = RuntimeThreadRunEventState()
    events = []
    for delta in ("a", "b", "c"):
        events.extend(
            await normalize_runtime_thread_raw_event(
                {
                    "message": {
                        "method": "item/reasoning/summaryTextDelta",
                        "params": {"itemId": "reason-1", "delta": delta},
                    }
                },
                state,
                timestamp="2026-03-19T00:00:00Z",
            )
        )

    persist_turn_timeline(
        tmp_path,
        execution_id="turn-reasoning",
        target_kind="thread_target",
        target_id="thread-1",
        events=events,
    )

    timeline = list_turn_timeline(tmp_path, execution_id="turn-reasoning")
    assert [entry["event"]["message"] for entry in timeline] == ["a", "b", "c"]
    assert timeline[1]["hot_coalesced_to_delta"] is True
    assert timeline[2]["hot_coalesced_to_delta"] is True

    checkpoint = ColdTraceStore(tmp_path).load_checkpoint("turn-reasoning")
    assert checkpoint is not None
    assert checkpoint.progress_text_kind == "thinking"
    assert checkpoint.progress_text_preview == "abc"
    assert checkpoint.progress_char_count == 3
    assert checkpoint.hot_projection_state["family_hot_rows"]["run_notice"] == 3


@pytest.mark.anyio
async def test_persist_turn_timeline_dedupes_repeated_agent_thought_chunk_messages(
    tmp_path: Path,
) -> None:
    state = RuntimeThreadRunEventState()
    events = []
    for content in ("thinking", "thinking more", "thinking more"):
        events.extend(
            await normalize_runtime_thread_raw_event(
                {
                    "message": {
                        "method": "session/update",
                        "params": {
                            "sessionId": "session-1",
                            "turnId": "turn-1",
                            "update": {
                                "sessionUpdate": "agent_thought_chunk",
                                "content": {"type": "text", "text": content},
                            },
                        },
                    }
                },
                state,
                timestamp="2026-03-19T00:00:01Z",
            )
        )

    persist_turn_timeline(
        tmp_path,
        execution_id="turn-thought-chunk",
        target_kind="thread_target",
        target_id="thread-1",
        events=events,
    )

    timeline = list_turn_timeline(tmp_path, execution_id="turn-thought-chunk")
    assert [entry["event"]["message"] for entry in timeline] == ["thinking", " more"]

    checkpoint = ColdTraceStore(tmp_path).load_checkpoint("turn-thought-chunk")
    assert checkpoint is not None
    assert checkpoint.progress_text_preview == "thinking more"
    assert checkpoint.progress_char_count == len("thinking more")
    assert checkpoint.hot_projection_state["deduped_counts"]["run_notice"] == 1


def test_persist_turn_timeline_bounds_hot_run_notice_growth_and_updates_checkpoint(
    tmp_path: Path,
) -> None:
    events = [
        RunNotice(
            timestamp=f"2026-03-19T00:{index // 60:02d}:{index % 60:02d}Z",
            kind="progress",
            message=f"notice {index}",
        )
        for index in range(150)
    ]

    count = persist_turn_timeline(
        tmp_path,
        execution_id="turn-notice-bounds",
        target_kind="thread_target",
        target_id="thread-1",
        events=events,
    )

    assert count == 150
    timeline = list_turn_timeline(tmp_path, execution_id="turn-notice-bounds")
    assert len(timeline) == 100
    assert timeline[-1]["event"]["message"] == "notice 99"

    checkpoint = ColdTraceStore(tmp_path).load_checkpoint("turn-notice-bounds")
    assert checkpoint is not None
    assert checkpoint.progress_text_preview == "notice 149"
    assert checkpoint.progress_char_count == len("notice 149")
    assert checkpoint.projection_event_cursor == 150
    assert checkpoint.hot_projection_state["family_hot_rows"]["run_notice"] == 100
    assert checkpoint.hot_projection_state["spilled_counts"]["run_notice"] == 50


def test_persist_turn_timeline_appends_from_start_index(tmp_path: Path) -> None:
    first_count = persist_turn_timeline(
        tmp_path,
        execution_id="turn-append",
        target_kind="thread_target",
        target_id="thread-1",
        events=[
            RunNotice(
                timestamp="2026-03-19T00:00:00Z",
                kind="thinking",
                message="planning",
            ),
            ToolCall(
                timestamp="2026-03-19T00:00:01Z",
                tool_name="shell",
                tool_input={"cmd": "pwd"},
            ),
        ],
    )
    second_count = persist_turn_timeline(
        tmp_path,
        execution_id="turn-append",
        target_kind="thread_target",
        target_id="thread-1",
        events=[
            ToolResult(
                timestamp="2026-03-19T00:00:02Z",
                tool_name="shell",
                status="completed",
                result={"stdout": "/tmp"},
            ),
            Completed(
                timestamp="2026-03-19T00:00:03Z",
                final_message="done",
            ),
        ],
        start_index=3,
    )

    assert first_count == 2
    assert second_count == 2
    timeline = list_turn_timeline(tmp_path, execution_id="turn-append")
    assert [entry["event_index"] for entry in timeline] == [1, 2, 3, 4]
    assert [entry["event_type"] for entry in timeline] == [
        "run_notice",
        "tool_call",
        "tool_result",
        "turn_completed",
    ]
