from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient
from tests.pma_support import _enable_pma, _repo_owner

from codex_autorunner.core.managed_thread_store import ManagedThreadStore
from codex_autorunner.core.orchestration import SQLiteChatSurfaceEventJournal
from codex_autorunner.core.orchestration.cold_trace_store import ColdTraceWriter
from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.core.orchestration.turn_timeline import (
    append_turn_events_to_cold_trace,
    list_turn_timeline,
    persist_turn_timeline,
)
from codex_autorunner.core.ports.run_event import Completed, OutputDelta
from codex_autorunner.server import create_hub_app


def test_managed_thread_timeline_endpoint_returns_canonical_items(hub_env) -> None:
    _enable_pma(
        hub_env.hub_root,
        managed_thread_terminal_followup_default=False,
    )
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

        store = ManagedThreadStore(hub_env.hub_root)
        turn = store.create_turn(managed_thread_id, prompt="hello timeline")
        assert store.mark_turn_finished(
            str(turn["managed_turn_id"]),
            status="ok",
            assistant_text="hello from assistant",
        )

        timeline_resp = client.get(f"/hub/pma/threads/{managed_thread_id}/timeline")

    assert timeline_resp.status_code == 200
    payload = timeline_resp.json()
    assert payload["contract_version"] == "managed_thread_timeline.v2"
    assert payload["projection"]["kind"] == "transcript"
    assert payload["projection"]["raw_trace_available"] is True
    assert [item["kind"] for item in payload["items"]] == [
        "user_message",
        "assistant_message",
        "status",
    ]
    assert payload["items"][0]["payload"]["text"] == "hello timeline"
    assert payload["items"][1]["payload"]["text"] == "hello from assistant"
    for item in payload["items"]:
        assert item["contract_version"] == "managed_thread_timeline.v2"
        assert item["identity"]["timeline_item_id"] == item["item_id"]
        assert isinstance(item["identity"]["progress_item_ids"], list)
        assert isinstance(item["provenance"]["source_event_ids"], list)
        assert isinstance(item["provenance"]["progress_event_ids"], list)


def test_managed_thread_timeline_endpoint_suppresses_output_deltas_but_keeps_raw_trace(
    hub_env,
) -> None:
    _enable_pma(
        hub_env.hub_root,
        managed_thread_terminal_followup_default=False,
    )
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

        store = ManagedThreadStore(hub_env.hub_root)
        turn = store.create_turn(managed_thread_id, prompt="stream chunks")
        turn_id = str(turn["managed_turn_id"])
        events = [
            OutputDelta(
                timestamp=f"2026-05-12T10:00:{index % 60:02d}Z",
                delta_type="assistant_stream",
                content=f"chunk {index}",
            )
            for index in range(750)
        ]
        events.append(
            Completed(
                timestamp="2026-05-12T10:15:00Z",
                final_message="finished",
            )
        )
        trace_writer = ColdTraceWriter(
            hub_root=hub_env.hub_root,
            execution_id=turn_id,
        ).open()
        try:
            append_turn_events_to_cold_trace(trace_writer, events=events)
            trace_writer.finalize()
        finally:
            trace_writer.close()
        persist_turn_timeline(
            hub_env.hub_root,
            execution_id=turn_id,
            target_kind="thread_target",
            target_id=managed_thread_id,
            events=events,
        )
        assert store.mark_turn_finished(
            turn_id,
            status="ok",
            assistant_text="finished",
        )

        timeline_resp = client.get(f"/hub/pma/threads/{managed_thread_id}/timeline")
        turn_resp = client.get(f"/hub/pma/threads/{managed_thread_id}/turns/{turn_id}")

    assert timeline_resp.status_code == 200
    payload = timeline_resp.json()
    assert [item["kind"] for item in payload["items"]] == [
        "user_message",
        "assistant_message",
        "status",
    ]
    assert payload["item_count"] == 3
    hot_timeline = list_turn_timeline(hub_env.hub_root, execution_id=turn_id)
    assert len(hot_timeline) > payload["item_count"]
    assert any(entry["event_type"] == "output_delta" for entry in hot_timeline)
    assert turn_resp.status_code == 200
    turn_detail = turn_resp.json()
    assert turn_detail["trace_metadata"]["hot_timeline_entries"] == len(hot_timeline)
    assert turn_detail["trace_metadata"]["cold_trace_available"] is True
    assert turn_detail["trace_metadata"]["cold_trace"]["event_count"] == 751


def test_managed_thread_timeline_endpoint_validates_and_clamps_limit(hub_env) -> None:
    _enable_pma(
        hub_env.hub_root,
        managed_thread_terminal_followup_default=False,
    )
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

        invalid_low = client.get(
            f"/hub/pma/threads/{managed_thread_id}/timeline",
            params={"limit": 0},
        )
        clamped_high = client.get(
            f"/hub/pma/threads/{managed_thread_id}/timeline",
            params={"limit": 201},
        )

    assert invalid_low.status_code == 422
    assert clamped_high.status_code == 200
    assert clamped_high.json()["projection"]["limit"] == 200


def test_managed_thread_chat_events_endpoint_returns_snapshot(hub_env) -> None:
    _enable_pma(
        hub_env.hub_root,
        managed_thread_terminal_followup_default=False,
    )
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

        events_resp = client.get("/hub/pma/events?once=true")

    assert events_resp.status_code == 200
    assert events_resp.headers["content-type"].startswith("text/event-stream")
    body = events_resp.text
    assert "event: chat_snapshot" in body
    assert f'"managed_thread_id": "{managed_thread_id}"' in body
    assert '"contract_version": "pma_chat_events.v1"' in body


def test_managed_thread_chat_events_endpoint_uses_generic_chat_lifecycle(
    hub_env,
) -> None:
    _enable_pma(
        hub_env.hub_root,
        managed_thread_terminal_followup_default=False,
    )
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

        event = (
            SQLiteChatSurfaceEventJournal(hub_env.hub_root, durable=True)
            .append_event(
                idempotency_key="pma-thread-queued",
                event_type="queue.state_changed",
                surface_kind="pma",
                surface_key=managed_thread_id,
                managed_thread_id=managed_thread_id,
                repo_id=hub_env.repo_id,
                status="queued",
            )
            .event
        )

        events_resp = client.get(
            "/hub/pma/events",
            params={"cursor": str(event.cursor - 1), "once": "true"},
        )

    assert events_resp.status_code == 200
    snapshot = _first_sse_json_payload(events_resp.text)
    thread = next(
        item
        for item in snapshot["threads"]
        if item["managed_thread_id"] == managed_thread_id
    )
    assert snapshot["cursor"] == event.cursor
    assert thread["runtime_status"] == "queued"


def test_managed_thread_chat_event_revision_tracks_visible_state_without_timestamp_bump(
    hub_env,
) -> None:
    _enable_pma(
        hub_env.hub_root,
        managed_thread_terminal_followup_default=False,
    )
    app = create_hub_app(hub_env.hub_root)

    with TestClient(app) as client:
        create_resp = client.post(
            "/hub/pma/threads",
            json={"agent": "codex", **_repo_owner(hub_env)},
        )
        assert create_resp.status_code == 200
        managed_thread_id = create_resp.json()["thread"]["managed_thread_id"]

        before_resp = client.get("/hub/pma/events?once=true")
        assert before_resp.status_code == 200
        before_snapshot = _first_sse_json_payload(before_resp.text)

        with open_orchestration_sqlite(hub_env.hub_root, migrate=True) as conn:
            conn.execute(
                """
                UPDATE orch_thread_targets
                   SET status_reason = ?
                 WHERE thread_target_id = ?
                """,
                ("same-second visible update", managed_thread_id),
            )

        after_resp = client.get("/hub/pma/events?once=true")
        assert after_resp.status_code == 200
        after_snapshot = _first_sse_json_payload(after_resp.text)

    assert after_snapshot["revision"] != before_snapshot["revision"]
    updated_thread = next(
        thread
        for thread in after_snapshot["threads"]
        if thread["managed_thread_id"] == managed_thread_id
    )
    assert updated_thread["status_reason"] == "same-second visible update"


def _first_sse_json_payload(body: str) -> dict[str, Any]:
    for line in body.splitlines():
        if line.startswith("data: "):
            return json.loads(line.removeprefix("data: "))
    raise AssertionError(f"No SSE data payload found in: {body!r}")
