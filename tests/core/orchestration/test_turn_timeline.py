from __future__ import annotations

from pathlib import Path

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
    assert timeline[0]["event"]["kind"] == "thinking"
    assert timeline[1]["event"]["tool_name"] == "shell"
    assert timeline[2]["event"]["result"] == {"stdout": "/tmp"}
    assert timeline[3]["event"]["final_message"] == "done"


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
