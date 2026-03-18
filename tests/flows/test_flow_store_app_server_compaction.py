from __future__ import annotations

import json

from codex_autorunner.core.flows.models import FlowEventType
from codex_autorunner.core.flows.store import FlowStore


def _create_run(store: FlowStore) -> str:
    record = store.create_flow_run(
        run_id="run-app-server-compaction",
        flow_type="ticket_flow",
        input_data={},
    )
    return record.id


def test_create_event_compacts_high_volume_app_server_tool_updates(tmp_path) -> None:
    store = FlowStore(tmp_path / "flows.db")
    store.initialize()
    run_id = _create_run(store)
    large_command = "echo hello\n" * 600
    original = {
        "turn_id": "turn-1",
        "message": {
            "method": "message.part.updated",
            "params": {
                "properties": {
                    "part": {
                        "id": "part-1",
                        "sessionID": "thread-1",
                        "messageID": "message-1",
                        "type": "tool",
                        "tool": "bash",
                        "command": large_command,
                        "state": {"status": "completed", "exitCode": 0},
                    }
                }
            },
        },
    }

    event = store.create_event(
        event_id="evt-tool",
        run_id=run_id,
        event_type=FlowEventType.APP_SERVER_EVENT,
        data=original,
    )

    assert event.data["truncated"] is True
    assert event.data["method"] == "message.part.updated"
    assert event.data["turn_id"] == "turn-1"
    assert event.data["thread_id"] == "thread-1"
    assert event.data["message_id"] == "message-1"
    assert event.data["part_id"] == "part-1"
    assert event.data["tool"] == "bash"
    assert event.data["payload_bytes"] == len(
        json.dumps(original, ensure_ascii=False).encode("utf-8")
    )
    assert len(event.data["preview"]) == 2048

    message = event.data["message"]
    params = message["params"]
    part = params["properties"]["part"]
    assert message["method"] == "message.part.updated"
    assert part["type"] == "tool"
    assert part["tool"] == "bash"
    assert part["command"] == large_command[:2048]
    assert part["state"] == {"status": "completed", "exitCode": 0}
    assert len(json.dumps(event.data)) < len(json.dumps(original))


def test_create_event_preserves_properties_delta_object_for_replay(tmp_path) -> None:
    store = FlowStore(tmp_path / "flows.db")
    store.initialize()
    run_id = _create_run(store)

    event = store.create_event(
        event_id="evt-delta-object",
        run_id=run_id,
        event_type=FlowEventType.APP_SERVER_EVENT,
        data={
            "message": {
                "method": "message.part.updated",
                "params": {
                    "properties": {
                        "info": {"id": "message-2", "role": "assistant"},
                        "part": {
                            "id": "part-2",
                            "messageID": "message-2",
                            "type": "text",
                            "text": "hello there",
                        },
                        "delta": {"text": " there"},
                    }
                },
            }
        },
    )

    properties = event.data["message"]["params"]["properties"]
    assert properties["delta"] == {"text": " there"}
    assert properties["part"]["text"] == "hello there"


def test_create_event_preserves_args_based_tool_input_for_replay(tmp_path) -> None:
    store = FlowStore(tmp_path / "flows.db")
    store.initialize()
    run_id = _create_run(store)

    event = store.create_event(
        event_id="evt-tool-args",
        run_id=run_id,
        event_type=FlowEventType.APP_SERVER_EVENT,
        data={
            "message": {
                "method": "message.part.updated",
                "params": {
                    "properties": {
                        "part": {
                            "id": "part-3",
                            "type": "tool",
                            "tool": "bash",
                            "args": {"command": "pwd"},
                            "state": {"status": "running"},
                        }
                    }
                },
            }
        },
    )

    part = event.data["message"]["params"]["properties"]["part"]
    assert part["args"] == {"command": "pwd"}
    assert event.data["preview"] == "pwd"


def test_create_event_compacts_session_diff_to_counts_and_status(tmp_path) -> None:
    store = FlowStore(tmp_path / "flows.db")
    store.initialize()
    run_id = _create_run(store)

    event = store.create_event(
        event_id="evt-session-diff",
        run_id=run_id,
        event_type=FlowEventType.APP_SERVER_EVENT,
        data={
            "message": {
                "method": "session.diff",
                "params": {
                    "properties": {
                        "sessionID": "thread-9",
                        "diff": [{"path": "a.py"}, {"path": "b.py"}],
                    }
                },
            }
        },
    )

    assert event.data["truncated"] is True
    assert event.data["method"] == "session.diff"
    assert event.data["thread_id"] == "thread-9"
    assert event.data["preview"] == "2 diff entries"
    assert event.data["message"]["params"]["status"] == "diff updated"
    assert event.data["message"]["params"]["properties"]["diff_count"] == 2


def test_create_event_keeps_low_volume_approval_payloads_raw(tmp_path) -> None:
    store = FlowStore(tmp_path / "flows.db")
    store.initialize()
    run_id = _create_run(store)
    original = {
        "turn_id": "turn-approve",
        "message": {
            "method": "item/commandExecution/requestApproval",
            "params": {
                "item": {"type": "commandExecution", "command": ["git", "status"]},
                "message": "Need approval",
            },
        },
    }

    event = store.create_event(
        event_id="evt-approval",
        run_id=run_id,
        event_type=FlowEventType.APP_SERVER_EVENT,
        data=original,
    )

    assert event.data == original
