from __future__ import annotations

import gzip
import json
import tempfile
from pathlib import Path

import pytest

import codex_autorunner.core.flows.store as flow_store_module
from codex_autorunner.core.flows.models import FlowEventType, FlowRunStatus
from codex_autorunner.core.flows.store import FlowStore
from codex_autorunner.core.flows.telemetry_export import (
    classify_events_for_run,
    export_all_runs,
    export_run,
)


@pytest.fixture
def temp_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


def _make_store(temp_dir: Path) -> FlowStore:
    store = FlowStore(temp_dir / "flows.db")
    store.initialize()
    return store


def _create_terminal_run(
    store: FlowStore,
    run_id: str = "run-terminal-1",
    status: FlowRunStatus = FlowRunStatus.COMPLETED,
) -> str:
    record = store.create_flow_run(
        run_id=run_id, flow_type="ticket_flow", input_data={}
    )
    store.update_flow_run_status(
        record.id, status=status, finished_at="2025-01-01T00:00:00Z"
    )
    return record.id


def _create_active_run(store: FlowStore, run_id: str = "run-active-1") -> str:
    record = store.create_flow_run(
        run_id=run_id, flow_type="ticket_flow", input_data={}
    )
    store.update_flow_run_status(
        record.id, status=FlowRunStatus.RUNNING, started_at="2025-01-01T00:00:00Z"
    )
    return record.id


def _add_event(
    store: FlowStore,
    run_id: str,
    event_type: FlowEventType,
    data: dict,
    event_id: str | None = None,
) -> None:
    store.create_event(
        event_id=event_id or f"evt-{event_type.value}-{data.get('turn_id', 't1')}",
        run_id=run_id,
        event_type=event_type,
        data=data,
    )


def test_classify_empty_run(temp_dir):
    store = _make_store(temp_dir)
    run_id = _create_terminal_run(store)
    events, ev_app_seqs, tel_app_seqs, prune_delta, retained = classify_events_for_run(
        store, run_id, is_terminal=True
    )
    assert events == []
    assert ev_app_seqs == []
    assert tel_app_seqs == []
    assert prune_delta == []
    assert retained == []


def test_classify_active_run_nothing_prunable(temp_dir):
    store = _make_store(temp_dir)
    run_id = _create_active_run(store)
    _add_event(
        store,
        run_id,
        FlowEventType.APP_SERVER_EVENT,
        {"message": {"method": "message.part.updated", "params": {}}, "turn_id": "t1"},
    )
    _add_event(
        store,
        run_id,
        FlowEventType.AGENT_STREAM_DELTA,
        {"delta": "hello", "turn_id": "t1"},
    )
    events, ev_app_seqs, tel_app_seqs, prune_delta, retained = classify_events_for_run(
        store, run_id, is_terminal=False
    )
    assert len(events) == 2
    assert ev_app_seqs == []
    assert tel_app_seqs == []
    assert prune_delta == []
    assert len(retained) == 2


def test_classify_terminal_run_prunes_non_high_signal_app_events(temp_dir):
    store = _make_store(temp_dir)
    run_id = _create_terminal_run(store)
    _add_event(
        store,
        run_id,
        FlowEventType.APP_SERVER_EVENT,
        {"message": {"method": "message.part.updated", "params": {}}, "turn_id": "t1"},
    )
    events, ev_app_seqs, tel_app_seqs, prune_delta, retained = classify_events_for_run(
        store, run_id, is_terminal=True
    )
    assert len(events) == 1
    assert len(ev_app_seqs) == 1
    assert tel_app_seqs == []
    assert len(retained) == 0


def test_classify_terminal_run_prunes_item_completed_after_export(temp_dir):
    store = _make_store(temp_dir)
    run_id = _create_terminal_run(store)
    _add_event(
        store,
        run_id,
        FlowEventType.APP_SERVER_EVENT,
        {
            "message": {
                "method": "item/completed",
                "params": {"item": {"type": "commandExecution", "command": "ls"}},
            },
            "turn_id": "t1",
        },
    )
    events, ev_app_seqs, tel_app_seqs, prune_delta, retained = classify_events_for_run(
        store, run_id, is_terminal=True
    )
    assert len(events) == 1
    assert len(ev_app_seqs) == 1
    assert tel_app_seqs == []
    assert len(retained) == 0


def test_classify_terminal_run_prunes_user_role_after_export(temp_dir):
    store = _make_store(temp_dir)
    run_id = _create_terminal_run(store)
    _add_event(
        store,
        run_id,
        FlowEventType.APP_SERVER_EVENT,
        {
            "message": {"method": "message.part.updated", "params": {}},
            "role": "user",
            "turn_id": "t1",
        },
    )
    events, ev_app_seqs, tel_app_seqs, prune_delta, retained = classify_events_for_run(
        store, run_id, is_terminal=True
    )
    assert len(events) == 1
    assert len(ev_app_seqs) == 1
    assert tel_app_seqs == []
    assert len(retained) == 0


def test_classify_terminal_run_prunes_app_events_and_deltas(temp_dir):
    store = _make_store(temp_dir)
    run_id = _create_terminal_run(store)
    _add_event(
        store,
        run_id,
        FlowEventType.APP_SERVER_EVENT,
        {
            "message": {
                "method": "item/completed",
                "params": {"item": {"type": "agentMessage"}},
            },
            "turn_id": "t1",
        },
    )
    _add_event(
        store,
        run_id,
        FlowEventType.AGENT_STREAM_DELTA,
        {"delta": "assistant text", "turn_id": "t1"},
    )
    events, ev_app_seqs, tel_app_seqs, prune_delta, retained = classify_events_for_run(
        store, run_id, is_terminal=True
    )
    assert len(events) == 2
    assert len(ev_app_seqs) == 1
    assert tel_app_seqs == []
    assert len(prune_delta) == 1
    assert len(retained) == 0


def test_classify_terminal_run_prunes_all_deltas(temp_dir):
    store = _make_store(temp_dir)
    run_id = _create_terminal_run(store)
    _add_event(
        store,
        run_id,
        FlowEventType.AGENT_STREAM_DELTA,
        {"delta": "orphan delta", "turn_id": "t1"},
    )
    events, ev_app_seqs, tel_app_seqs, prune_delta, retained = classify_events_for_run(
        store, run_id, is_terminal=True
    )
    assert len(events) == 1
    assert ev_app_seqs == []
    assert tel_app_seqs == []
    assert len(prune_delta) == 1
    assert len(retained) == 0


def test_export_run_skips_active(temp_dir):
    store = _make_store(temp_dir)
    run_id = _create_active_run(store)
    record = store.get_flow_run(run_id)
    assert record is not None
    result = export_run(temp_dir, store, record, dry_run=False)
    assert result.skipped is True
    assert "active" in (result.skip_reason or "")


def test_export_run_skips_empty_terminal(temp_dir):
    store = _make_store(temp_dir)
    run_id = _create_terminal_run(store)
    record = store.get_flow_run(run_id)
    assert record is not None
    result = export_run(temp_dir, store, record, dry_run=False)
    assert result.skipped is True
    assert "no wire telemetry" in (result.skip_reason or "")


def test_export_run_dry_run(temp_dir):
    store = _make_store(temp_dir)
    run_id = _create_terminal_run(store)
    _add_event(
        store,
        run_id,
        FlowEventType.APP_SERVER_EVENT,
        {"message": {"method": "message.part.updated", "params": {}}, "turn_id": "t1"},
    )
    record = store.get_flow_run(run_id)
    assert record is not None
    result = export_run(temp_dir, store, record, dry_run=True)
    assert result.skipped is False
    assert result.exported_events == 1
    assert result.prunable_app_server_events == 1
    assert result.exported_bytes > 0

    archive_path = temp_dir / ".codex-autorunner" / "flows" / run_id
    assert not archive_path.exists()


def test_export_run_creates_archive(temp_dir):
    store = _make_store(temp_dir)
    run_id = _create_terminal_run(store)
    _add_event(
        store,
        run_id,
        FlowEventType.APP_SERVER_EVENT,
        {"message": {"method": "message.part.updated", "params": {}}, "turn_id": "t1"},
        event_id="evt-app-1",
    )
    _add_event(
        store,
        run_id,
        FlowEventType.AGENT_STREAM_DELTA,
        {"delta": "hello", "turn_id": "t1"},
        event_id="evt-delta-1",
    )
    record = store.get_flow_run(run_id)
    assert record is not None

    result = export_run(temp_dir, store, record, dry_run=False)
    assert result.skipped is False
    assert result.exported_events == 2
    assert result.archive_path is not None

    archive_path = Path(result.archive_path)
    assert archive_path.exists()
    assert archive_path.suffix == ".gz"

    with gzip.open(archive_path, "rb") as f:
        lines = f.read().decode("utf-8").strip().split("\n")
    assert len(lines) == 2
    parsed = json.loads(lines[0])
    assert parsed["event_type"] == "app_server_event"
    parsed2 = json.loads(lines[1])
    assert parsed2["event_type"] == "agent_stream_delta"


def test_export_run_prunes_redundant_rows(temp_dir):
    store = _make_store(temp_dir)
    run_id = _create_terminal_run(store)
    _add_event(
        store,
        run_id,
        FlowEventType.APP_SERVER_EVENT,
        {"message": {"method": "message.part.updated", "params": {}}, "turn_id": "t1"},
        event_id="evt-app-1",
    )
    _add_event(
        store,
        run_id,
        FlowEventType.AGENT_STREAM_DELTA,
        {"delta": "delta-text", "turn_id": "t1"},
        event_id="evt-delta-1",
    )

    all_events_before = store.get_events(run_id)
    assert len(all_events_before) == 2

    record = store.get_flow_run(run_id)
    assert record is not None
    result = export_run(temp_dir, store, record, dry_run=False)
    assert result.prunable_app_server_events == 1
    assert result.prunable_stream_deltas == 1

    all_events_after = store.get_events(run_id)
    assert len(all_events_after) == 0


def test_export_run_prunes_all_terminal_wire_events(temp_dir):
    store = _make_store(temp_dir)
    run_id = _create_terminal_run(store)
    _add_event(
        store,
        run_id,
        FlowEventType.APP_SERVER_EVENT,
        {
            "message": {
                "method": "item/completed",
                "params": {"item": {"type": "commandExecution", "command": "ls"}},
            },
            "turn_id": "t1",
        },
        event_id="evt-high-1",
    )
    _add_event(
        store,
        run_id,
        FlowEventType.AGENT_STREAM_DELTA,
        {"delta": "command output", "turn_id": "t1"},
        event_id="evt-delta-1",
    )

    record = store.get_flow_run(run_id)
    assert record is not None
    result = export_run(temp_dir, store, record, dry_run=False)
    assert result.prunable_app_server_events == 1
    assert result.prunable_stream_deltas == 1
    assert result.retained_events == 0

    remaining = store.get_events(run_id)
    assert len(remaining) == 0


def test_export_all_runs_mixed(temp_dir):
    store = _make_store(temp_dir)
    active_id = _create_active_run(store, "run-active")
    terminal_id = _create_terminal_run(store, "run-terminal")

    _add_event(
        store,
        active_id,
        FlowEventType.APP_SERVER_EVENT,
        {"message": {"method": "message.part.updated", "params": {}}, "turn_id": "t1"},
        event_id="evt-active-1",
    )
    _add_event(
        store,
        terminal_id,
        FlowEventType.APP_SERVER_EVENT,
        {"message": {"method": "message.part.updated", "params": {}}, "turn_id": "t1"},
        event_id="evt-terminal-1",
    )

    result = export_all_runs(temp_dir, store, dry_run=False)
    assert len(result.records) == 2
    skipped = [r for r in result.records if r.skipped]
    exported = [r for r in result.records if not r.skipped]
    assert len(skipped) == 1
    assert len(exported) == 1
    assert exported[0].exported_events == 1


def test_export_all_runs_specific_run_ids(temp_dir):
    store = _make_store(temp_dir)
    _create_terminal_run(store, "run-1")
    _create_terminal_run(store, "run-2")

    _add_event(
        store,
        "run-1",
        FlowEventType.APP_SERVER_EVENT,
        {"message": {"method": "message.part.updated", "params": {}}, "turn_id": "t1"},
        event_id="evt-run1-1",
    )
    _add_event(
        store,
        "run-2",
        FlowEventType.APP_SERVER_EVENT,
        {"message": {"method": "message.part.updated", "params": {}}, "turn_id": "t1"},
        event_id="evt-run2-1",
    )
    _add_event(
        store,
        "run-2",
        FlowEventType.APP_SERVER_EVENT,
        {"message": {"method": "message.part.updated", "params": {}}, "turn_id": "t1"},
    )

    result = export_all_runs(temp_dir, store, dry_run=False, run_ids=["run-1"])
    assert len(result.records) == 1
    assert result.records[0].run_id == "run-1"


def test_export_all_runs_dry_run_summary(temp_dir):
    store = _make_store(temp_dir)
    _create_terminal_run(store, "run-terminal")
    _add_event(
        store,
        "run-terminal",
        FlowEventType.APP_SERVER_EVENT,
        {"message": {"method": "message.part.updated", "params": {}}, "turn_id": "t1"},
    )

    result = export_all_runs(temp_dir, store, dry_run=True)
    summary = result.dry_run_summary()
    assert summary["runs_total"] == 1
    assert summary["events_to_export"] == 1
    assert summary["events_to_prune"] == 1


def test_export_run_responses_style_telemetry(temp_dir):
    store = _make_store(temp_dir)
    run_id = _create_terminal_run(store)
    _add_event(
        store,
        run_id,
        FlowEventType.APP_SERVER_EVENT,
        {
            "message": {
                "method": "message.part.updated",
                "params": {
                    "properties": {
                        "part": {
                            "type": "text",
                            "text": "reasoning step 1",
                        }
                    },
                    "delta": {"text": "reasoning step 1"},
                },
            },
            "turn_id": "t1",
        },
        event_id="evt-responses-1",
    )
    _add_event(
        store,
        run_id,
        FlowEventType.AGENT_STREAM_DELTA,
        {"delta": "reasoning step 1", "turn_id": "t1"},
        event_id="evt-delta-responses-1",
    )

    record = store.get_flow_run(run_id)
    assert record is not None
    result = export_run(temp_dir, store, record, dry_run=False)
    assert result.exported_events == 2
    assert result.prunable_app_server_events == 1
    assert result.prunable_stream_deltas == 1


def test_export_run_assistants_style_item_completed(temp_dir):
    store = _make_store(temp_dir)
    run_id = _create_terminal_run(store)
    _add_event(
        store,
        run_id,
        FlowEventType.APP_SERVER_EVENT,
        {
            "message": {
                "method": "item/completed",
                "params": {
                    "item": {
                        "type": "agentMessage",
                        "text": "final answer",
                    }
                },
            },
            "turn_id": "t1",
        },
        event_id="evt-assistants-1",
    )
    _add_event(
        store,
        run_id,
        FlowEventType.AGENT_STREAM_DELTA,
        {"delta": "streamed chunk", "turn_id": "t1"},
        event_id="evt-delta-assistants-1",
    )

    record = store.get_flow_run(run_id)
    assert record is not None
    result = export_run(temp_dir, store, record, dry_run=False)
    assert result.exported_events == 2
    assert result.prunable_app_server_events == 1
    assert result.prunable_stream_deltas == 1
    assert result.retained_events == 0


def _add_telemetry(
    store: FlowStore,
    run_id: str,
    event_type: FlowEventType,
    data: dict,
    telemetry_id: str | None = None,
) -> None:
    store.create_telemetry(
        telemetry_id=telemetry_id
        or f"tel-{event_type.value}-{data.get('turn_id', 't1')}",
        run_id=run_id,
        event_type=event_type,
        data=data,
    )


def test_classify_terminal_run_includes_telemetry_table(temp_dir):
    store = _make_store(temp_dir)
    run_id = _create_terminal_run(store)
    _add_telemetry(
        store,
        run_id,
        FlowEventType.APP_SERVER_EVENT,
        {"message": {"method": "message.part.updated", "params": {}}, "turn_id": "t1"},
        telemetry_id="tel-app-1",
    )
    events, ev_app_seqs, tel_app_seqs, prune_delta, retained = classify_events_for_run(
        store, run_id, is_terminal=True
    )
    assert len(events) == 1
    assert events[0]["source"] == "flow_telemetry"
    assert ev_app_seqs == []
    assert len(tel_app_seqs) == 1
    assert prune_delta == []
    assert len(retained) == 0


def test_export_run_prunes_telemetry_rows(temp_dir):
    store = _make_store(temp_dir)
    run_id = _create_terminal_run(store)
    _add_telemetry(
        store,
        run_id,
        FlowEventType.APP_SERVER_EVENT,
        {"message": {"method": "message.part.updated", "params": {}}, "turn_id": "t1"},
        telemetry_id="tel-app-1",
    )

    record = store.get_flow_run(run_id)
    assert record is not None
    result = export_run(temp_dir, store, record, dry_run=False)
    assert result.exported_events == 1
    assert result.prunable_app_server_events == 1
    assert result.archive_path is not None

    remaining = store.get_telemetry(run_id)
    assert len(remaining) == 0


def test_export_run_handles_both_tables(temp_dir):
    store = _make_store(temp_dir)
    run_id = _create_terminal_run(store)
    _add_event(
        store,
        run_id,
        FlowEventType.APP_SERVER_EVENT,
        {"message": {"method": "message.part.updated", "params": {}}, "turn_id": "t1"},
        event_id="evt-historical-1",
    )
    _add_telemetry(
        store,
        run_id,
        FlowEventType.APP_SERVER_EVENT,
        {"message": {"method": "message.part.updated", "params": {}}, "turn_id": "t2"},
        telemetry_id="tel-new-1",
    )
    _add_event(
        store,
        run_id,
        FlowEventType.AGENT_STREAM_DELTA,
        {"delta": "chunk", "turn_id": "t1"},
        event_id="evt-delta-1",
    )

    record = store.get_flow_run(run_id)
    assert record is not None
    result = export_run(temp_dir, store, record, dry_run=False)
    assert result.exported_events == 3
    assert result.prunable_app_server_events == 2
    assert result.prunable_stream_deltas == 1

    remaining_events = store.get_events(run_id)
    assert len(remaining_events) == 0
    remaining_telemetry = store.get_telemetry(run_id)
    assert len(remaining_telemetry) == 0


def test_export_run_prunes_large_seq_sets_in_batches(temp_dir, monkeypatch):
    monkeypatch.setattr(flow_store_module, "_DELETE_SEQ_BATCH_SIZE", 2)

    store = _make_store(temp_dir)
    run_id = _create_terminal_run(store)

    for idx in range(5):
        _add_event(
            store,
            run_id,
            FlowEventType.APP_SERVER_EVENT,
            {
                "message": {
                    "method": "message.part.updated",
                    "params": {"index": idx},
                },
                "turn_id": f"t-app-{idx}",
            },
            event_id=f"evt-app-{idx}",
        )

    for idx in range(4):
        _add_event(
            store,
            run_id,
            FlowEventType.AGENT_STREAM_DELTA,
            {"delta": f"chunk-{idx}", "turn_id": f"t-delta-{idx}"},
            event_id=f"evt-delta-{idx}",
        )

    for idx in range(3):
        _add_telemetry(
            store,
            run_id,
            FlowEventType.APP_SERVER_EVENT,
            {
                "message": {
                    "method": "message.part.updated",
                    "params": {"index": idx},
                },
                "turn_id": f"t-tel-{idx}",
            },
            telemetry_id=f"tel-app-{idx}",
        )

    record = store.get_flow_run(run_id)
    assert record is not None

    result = export_run(temp_dir, store, record, dry_run=False)
    assert result.prunable_app_server_events == 8
    assert result.prunable_stream_deltas == 4
    assert store.get_events(run_id) == []
    assert store.get_telemetry(run_id) == []
