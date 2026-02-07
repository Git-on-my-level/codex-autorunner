from __future__ import annotations

from codex_autorunner.core.flows.failure_diagnostics import build_failure_payload
from codex_autorunner.core.flows.models import FlowEventType
from codex_autorunner.core.flows.store import FlowStore


def test_build_failure_payload_uses_newest_app_server_events(tmp_path) -> None:
    store = FlowStore(tmp_path / "flows.db")
    store.initialize()
    record = store.create_flow_run(
        run_id="run-failure-diag",
        flow_type="ticket_flow",
        input_data={},
    )

    for idx in range(250):
        store.create_event(
            event_id=f"evt-{idx}",
            run_id=record.id,
            event_type=FlowEventType.APP_SERVER_EVENT,
            data={
                "message": {
                    "method": "item/completed",
                    "params": {
                        "item": {
                            "type": "commandExecution",
                            "command": f"cmd-{idx}",
                            "exitCode": idx,
                            "stderr": f"stderr-{idx}",
                        }
                    },
                }
            },
        )

    payload = build_failure_payload(record, store=store)

    assert payload["last_command"] == "cmd-249"
    assert payload["exit_code"] == 249
    assert payload["stderr_tail"] == "stderr-249"
