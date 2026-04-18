from __future__ import annotations

import codex_autorunner.core.flows.store as flow_store_module
from codex_autorunner.core.flows.models import FlowEventType
from codex_autorunner.core.flows.store import FlowStore


def _create_run(store: FlowStore, run_id: str) -> str:
    record = store.create_flow_run(
        run_id=run_id, flow_type="ticket_flow", input_data={}
    )
    return record.id


def test_live_cap_keeps_latest_agent_stream_deltas(tmp_path, monkeypatch) -> None:
    monkeypatch.setitem(
        flow_store_module._FLOW_EVENT_TYPE_LIVE_CAPS,
        FlowEventType.AGENT_STREAM_DELTA.value,
        3,
    )
    store = FlowStore(tmp_path / "flows.db")
    store.initialize()
    run_id = _create_run(store, "run-deltas")

    for idx in range(6):
        store.create_event(
            event_id=f"evt-delta-{idx}",
            run_id=run_id,
            event_type=FlowEventType.AGENT_STREAM_DELTA,
            data={"delta": f"chunk-{idx}", "turn_id": "turn-1"},
        )

    kept = store.get_events_by_type(run_id, FlowEventType.AGENT_STREAM_DELTA)
    assert [event.id for event in kept] == ["evt-delta-3", "evt-delta-4", "evt-delta-5"]


def test_live_cap_keeps_latest_app_server_telemetry_per_run(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setitem(
        flow_store_module._FLOW_TELEMETRY_TYPE_LIVE_CAPS,
        FlowEventType.APP_SERVER_EVENT.value,
        2,
    )
    store = FlowStore(tmp_path / "flows.db")
    store.initialize()
    run_a = _create_run(store, "run-telemetry-a")
    run_b = _create_run(store, "run-telemetry-b")

    for idx in range(4):
        for run_id in (run_a, run_b):
            store.create_telemetry(
                telemetry_id=f"tel-{run_id}-{idx}",
                run_id=run_id,
                event_type=FlowEventType.APP_SERVER_EVENT,
                data={
                    "message": {
                        "method": "message.part.updated",
                        "params": {"idx": idx},
                    },
                    "turn_id": f"turn-{idx}",
                },
            )

    kept_a = store.get_telemetry_by_type(run_a, FlowEventType.APP_SERVER_EVENT)
    kept_b = store.get_telemetry_by_type(run_b, FlowEventType.APP_SERVER_EVENT)
    assert [event.id for event in kept_a] == [
        "tel-run-telemetry-a-2",
        "tel-run-telemetry-a-3",
    ]
    assert [event.id for event in kept_b] == [
        "tel-run-telemetry-b-2",
        "tel-run-telemetry-b-3",
    ]


def test_live_caps_do_not_prune_non_wire_event_types(tmp_path, monkeypatch) -> None:
    monkeypatch.setitem(
        flow_store_module._FLOW_EVENT_TYPE_LIVE_CAPS,
        FlowEventType.AGENT_STREAM_DELTA.value,
        1,
    )
    store = FlowStore(tmp_path / "flows.db")
    store.initialize()
    run_id = _create_run(store, "run-progress")

    for idx in range(5):
        store.create_event(
            event_id=f"evt-progress-{idx}",
            run_id=run_id,
            event_type=FlowEventType.STEP_PROGRESS,
            data={"progress": idx},
        )

    kept = store.get_events_by_type(run_id, FlowEventType.STEP_PROGRESS)
    assert len(kept) == 5
