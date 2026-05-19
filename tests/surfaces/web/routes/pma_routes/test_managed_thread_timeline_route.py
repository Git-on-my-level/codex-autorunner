from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient
from tests.pma_support import _enable_pma, _repo_owner

from codex_autorunner.core.managed_thread_store import ManagedThreadStore
from codex_autorunner.core.orchestration import SQLiteChatSurfaceEventJournal
from codex_autorunner.core.orchestration.cold_trace_store import ColdTraceWriter
from codex_autorunner.core.orchestration.managed_thread_transcript import (
    _merge_transcript_rows,
    build_managed_thread_transcript,
)
from codex_autorunner.core.orchestration.sqlite import open_orchestration_sqlite
from codex_autorunner.core.orchestration.turn_timeline import (
    append_turn_events_to_cold_trace,
    list_turn_timeline,
    persist_turn_timeline,
)
from codex_autorunner.core.ports.run_event import Completed, OutputDelta, RunNotice
from codex_autorunner.server import create_hub_app
from codex_autorunner.surfaces.web.routes.pma_routes.tail_stream import (
    _successful_terminal_turn_id,
    _transcript_append_frames,
    _transcript_has_assistant_row,
)


def test_transcript_stream_terminal_snapshot_gate_requires_assistant_row() -> None:
    assert (
        _successful_terminal_turn_id(
            {
                "terminal": True,
                "turn_status": "ok",
                "managed_turn_id": "turn-1",
            }
        )
        == "turn-1"
    )
    assert (
        _successful_terminal_turn_id(
            {
                "terminal": True,
                "turn_status": "failed",
                "managed_turn_id": "turn-1",
            }
        )
        is None
    )
    assert not _transcript_has_assistant_row(
        {
            "rows": [
                {
                    "kind": "message",
                    "turn_id": "turn-1",
                    "message": {"role": "user", "text": "hello"},
                }
            ]
        },
        "turn-1",
    )
    assert _transcript_has_assistant_row(
        {
            "rows": [
                {
                    "kind": "message",
                    "turn_id": "turn-1",
                    "message": {"role": "assistant", "text": "answer"},
                }
            ]
        },
        "turn-1",
    )


def test_transcript_append_frames_chunk_without_advancing_cursor_early() -> None:
    rows = [
        {
            "kind": "intermediate",
            "id": f"row-{index}",
            "order_key": f"{index:04d}",
        }
        for index in range(85)
    ]

    frames = list(_transcript_append_frames(rows, append_event_id=123, chunk_limit=80))

    assert len(frames) == 2
    assert "\nid: 123\n" not in frames[0]
    assert "\nid: 123\n" in frames[1]
    first_payload = json.loads(frames[0].split("data: ", 1)[1])
    second_payload = json.loads(frames[1].split("data: ", 1)[1])
    assert [row["id"] for row in first_payload["rows"]] == [
        f"row-{index}" for index in range(80)
    ]
    assert [row["id"] for row in second_payload["rows"]] == [
        f"row-{index}" for index in range(80, 85)
    ]


def test_managed_thread_transcript_live_rows_replace_stale_durable_rows() -> None:
    durable_rows = [
        {
            "kind": "intermediate",
            "id": "turn:1:intermediate:event-1",
            "order_key": "001",
            "text": "stale",
        }
    ]
    live_rows = [
        {
            "kind": "intermediate",
            "id": "turn:1:intermediate:event-1",
            "order_key": "001",
            "text": "fresh",
        }
    ]

    merged = _merge_transcript_rows(durable_rows, live_rows)

    assert merged == [live_rows[0]]


def test_managed_thread_transcript_live_rows_stay_after_turn_user_row() -> None:
    durable_rows = [
        {
            "kind": "message",
            "id": "turn:turn-1:user",
            "turn_id": "turn-1",
            "order_key": "00000005|2026-05-12T10:00:00Z|turn:turn-1:user",
            "message": {"role": "user", "text": "hello"},
        }
    ]
    live_rows = [
        {
            "kind": "intermediate",
            "id": "turn:turn-1:intermediate:0001",
            "turn_id": "turn-1",
            "order_key": "00000001|2026-05-12T09:59:59Z|progress",
            "text": "thinking",
        }
    ]

    merged = _merge_transcript_rows(durable_rows, live_rows)

    assert [row["id"] for row in merged] == [
        "turn:turn-1:user",
        "turn:turn-1:intermediate:0001",
    ]


def test_managed_thread_transcript_running_first_turn_orders_live_progress_after_user(
    hub_env,
) -> None:
    store = ManagedThreadStore(hub_env.hub_root)
    created = store.create_thread(
        "codex",
        hub_env.repo_root.resolve(),
        repo_id=hub_env.repo_id,
    )
    managed_thread_id = str(created["managed_thread_id"])
    turn = store.create_turn(managed_thread_id, prompt="first prompt")
    turn_id = str(turn["managed_turn_id"])

    transcript = build_managed_thread_transcript(
        hub_env.hub_root,
        thread_store=store,
        managed_thread_id=managed_thread_id,
        progress_snapshot={
            "managed_turn_id": turn_id,
            "last_event_id": 1,
            "events": [
                {
                    "event_id": 1,
                    "event_type": "progress",
                    "received_at": "2026-05-12T09:59:59Z",
                    "summary": "Planning next step",
                    "progress_kind": "assistant_update",
                    "progress_state": "running",
                    "progress_item": {
                        "item_id": "progress:assistant_update:1",
                        "kind": "assistant_update",
                        "summary": "Planning next step",
                        "event_ids": [1],
                    },
                }
            ],
        },
    )

    rows = transcript["rows"]
    assert [row["kind"] for row in rows] == ["message", "intermediate"]
    assert rows[0]["id"] == f"turn:{turn_id}:user"
    assert rows[0]["message"]["role"] == "user"
    assert rows[1]["id"] == f"turn:{turn_id}:intermediate:0001"
    assert rows[1]["turn_id"] == turn_id


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
    assert payload["contract_version"] == "managed_thread_timeline.v3"
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
        assert item["contract_version"] == "managed_thread_timeline.v3"
        assert item["identity"]["timeline_item_id"] == item["item_id"]
        assert isinstance(item["identity"]["progress_item_ids"], list)
        assert isinstance(item["provenance"]["source_event_ids"], list)
        assert isinstance(item["provenance"]["progress_event_ids"], list)


def test_managed_thread_transcript_endpoint_returns_backend_owned_rows(hub_env) -> None:
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
        turn = store.create_turn(managed_thread_id, prompt="hello transcript")
        turn_id = str(turn["managed_turn_id"])
        assert store.mark_turn_finished(
            turn_id,
            status="ok",
            assistant_text="hello from transcript",
        )

        transcript_resp = client.get(f"/hub/pma/threads/{managed_thread_id}/transcript")

    assert transcript_resp.status_code == 200
    payload = transcript_resp.json()
    assert payload["contract_version"] == "managed_thread_transcript.v2"
    assert payload["projection"]["kind"] == "transcript"
    assert payload["projection"]["backend_owned_rows"] is True
    rows = payload["rows"]
    assert [row["kind"] for row in rows] == ["message", "message"]
    assert rows[0]["id"] == f"turn:{turn_id}:user"
    assert rows[0]["message"]["role"] == "user"
    assert rows[0]["message"]["text"] == "hello transcript"
    assert rows[1]["message"]["role"] == "assistant"
    assert rows[1]["message"]["text"] == "hello from transcript"


def test_managed_thread_transcript_endpoint_exposes_capsule_visibility_contract(
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
        turn = store.create_turn(
            managed_thread_id,
            prompt="<injected context>\nrepo guidance\n</injected context>\n\nFix login",
            metadata={
                "raw_model_prompt": (
                    "<injected context>\nrepo guidance\n</injected context>\n\nFix login"
                ),
                "user_visible_text": "Fix login",
                "title_seed": "Fix login",
                "capsule_refs": [
                    {
                        "capsule_id": "car.repo_basics",
                        "capsule_version": "1",
                        "visibility": "model_only",
                        "scope": "repo",
                        "source_digest": "sha256:repo",
                    }
                ],
            },
        )
        turn_id = str(turn["managed_turn_id"])
        assert store.mark_turn_finished(
            turn_id,
            status="ok",
            assistant_text="done",
        )

        transcript_resp = client.get(f"/hub/pma/threads/{managed_thread_id}/transcript")

    assert transcript_resp.status_code == 200
    payload = transcript_resp.json()
    assert payload["contract_version"] == "managed_thread_transcript.v2"
    user_row = payload["rows"][0]
    assert user_row["message"]["text"] == "Fix login"
    assert user_row["visibility"] == "user_visible"
    assert user_row["user_visible_text"] == "Fix login"
    assert user_row["capsule_refs"] == [
        {
            "capsule_id": "car.repo_basics",
            "capsule_version": "1",
            "visibility": "model_only",
            "scope": "repo",
            "source_digest": "sha256:repo",
        }
    ]
    assert user_row["message"]["capsule_refs"] == user_row["capsule_refs"]


def test_managed_thread_transcript_includes_client_turn_correlation_fields(
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
        client_turn_id = "client-turn-correlation-123"
        turn = store.create_turn(
            managed_thread_id,
            prompt="hello correlation",
            client_turn_id=client_turn_id,
        )
        turn_id = str(turn["managed_turn_id"])
        assert store.mark_turn_finished(
            turn_id,
            status="ok",
            assistant_text="correlated answer",
        )

        transcript_resp = client.get(f"/hub/pma/threads/{managed_thread_id}/transcript")

    assert transcript_resp.status_code == 200
    rows = transcript_resp.json()["rows"]
    user_row = rows[0]
    assert user_row["id"] == f"turn:{turn_id}:user"
    assert user_row["client_turn_id"] == client_turn_id
    assert user_row["correlation_id"] == client_turn_id
    assert user_row["identity"]["correlation_id"] == client_turn_id
    assert user_row["message"]["client_turn_id"] == client_turn_id
    assert user_row["message"]["correlation_id"] == client_turn_id
    assert user_row["message"]["identity"]["correlation_id"] == client_turn_id


def test_managed_thread_transcript_endpoint_preserves_user_anchor_under_noise(
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
        turn = store.create_turn(managed_thread_id, prompt="anchor prompt")
        turn_id = str(turn["managed_turn_id"])
        events = [
            OutputDelta(
                timestamp=f"2026-05-12T10:00:{index % 60:02d}Z",
                delta_type="assistant_stream",
                content=f"chunk {index}",
            )
            for index in range(250)
        ]
        events.append(
            Completed(
                timestamp="2026-05-12T10:15:00Z",
                final_message="anchor answer",
            )
        )
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
            assistant_text="anchor answer",
        )

        transcript_resp = client.get(
            f"/hub/pma/threads/{managed_thread_id}/transcript",
            params={"limit": 3},
        )

    assert transcript_resp.status_code == 200
    rows = transcript_resp.json()["rows"]
    assert rows[0]["id"] == f"turn:{turn_id}:user"
    assert rows[0]["message"]["text"] == "anchor prompt"
    assert any(
        row["kind"] == "message"
        and row["message"]["role"] == "assistant"
        and row["message"]["text"] == "anchor answer"
        for row in rows
    )


def test_managed_thread_transcript_events_endpoint_returns_snapshot(hub_env) -> None:
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
        turn = store.create_turn(managed_thread_id, prompt="stream transcript")
        turn_id = str(turn["managed_turn_id"])
        persist_turn_timeline(
            hub_env.hub_root,
            execution_id=turn_id,
            target_kind="thread_target",
            target_id=managed_thread_id,
            events=[
                RunNotice(
                    timestamp="2026-05-12T10:00:00Z",
                    kind="plan",
                    message="stream setup",
                ),
            ],
        )
        assert store.mark_turn_finished(
            turn_id,
            status="ok",
            assistant_text="stream answer",
        )

        events_resp = client.get(
            f"/hub/pma/threads/{managed_thread_id}/transcript/events",
            params={"once": True},
        )

    assert events_resp.status_code == 200
    assert events_resp.headers["content-type"].startswith("text/event-stream")
    body = events_resp.text
    assert "event: transcript.snapshot" in body
    assert "\nid: " in body
    assert '"contract_version": "managed_thread_transcript.v2"' in body
    assert '"backend_owned_rows": true' in body


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
